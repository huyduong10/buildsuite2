# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

"""BOQ operations that aren't a single-document save — workflow transitions,
Assembly explosion, revisions, clone, template import and actuals recompute.
Reads go through the standard data adapter; only these mutations need the API.
"""

import frappe
from frappe import _
from frappe.utils import flt, today

from buildsuite_core.buildsuite_core.doctype.boq.boq_rollup import recompute_boq
from buildsuite_core.permissions.setup import BOQ_APPROVE_ROLES


def _require_write(boq):
	if not frappe.has_permission("BOQ", "write", doc=boq):
		frappe.throw(_("You are not permitted to modify this BOQ."), frappe.PermissionError)


def _require_draft(doc):
	if doc.status != "Draft":
		frappe.throw(_("Only a Draft BOQ can be edited — this one is {0}.").format(doc.status))


# --- workflow -------------------------------------------------------------


@frappe.whitelist()
def submit_boq(boq: str):
	"""Draft -> Submitted."""
	_require_write(boq)
	doc = frappe.get_doc("BOQ", boq)
	if doc.status != "Draft":
		frappe.throw(_("Only a Draft BOQ can be submitted for approval."))
	doc.status = "Submitted"
	doc.save(ignore_permissions=True)
	return doc.status


def _boq_codes(boq):
	"""(group_codes, item_codes) present in a BOQ revision — the join keys."""
	groups = [c for c in frappe.get_all("BOQ Group", filters={"boq": boq}, pluck="code") if c]
	items = [c for c in frappe.get_all("BOQ Item", filters={"boq": boq}, pluck="code") if c]
	return groups, items


def _validate_codes_for_approval(doc):
	"""R9 — the requirement that makes the rest true. Cost code is the only join key, so every
	other rule assumes codes are stable. Block approval if two lines share a code, or if a code
	that carries actuals in the current Approved revision is absent from this one (quietly
	renumbered/dropped rather than retired with its code kept)."""
	groups, items = _boq_codes(doc.name)
	dupes = sorted({c for c in groups if groups.count(c) > 1} | {c for c in items if items.count(c) > 1})
	if dupes:
		frappe.throw(
			_("Duplicate cost codes in this BOQ: {0}. Every line needs a unique code.").format(
				", ".join(dupes)
			)
		)

	prior = frappe.db.get_value(
		"BOQ", {"project": doc.project, "status": "Approved", "name": ("!=", doc.name)}, "name"
	)
	if not prior:
		return
	from buildsuite_core.api.boq_actuals import _actual_entries

	prior_codes = set().union(*[set(x) for x in _boq_codes(prior)])
	with_actuals = {
		c
		for e in _actual_entries(doc.project)
		for c in (e["group_code"], e["item_code"])
		if c and c in prior_codes
	}
	missing = sorted(with_actuals - (set(groups) | set(items)))
	if missing:
		frappe.throw(
			_(
				"These cost codes carry actuals in the current Approved revision but are absent "
				"from this one: {0}. Keep the code (retire the line) or restore it — codes are "
				"immutable once approved."
			).format(", ".join(missing))
		)


@frappe.whitelist()
def approve_boq(boq: str):
	"""Approve a Submitted BOQ; supersede any other Approved revision of the same
	project. Gated on BOQ_APPROVE_ROLES (server-authoritative)."""
	if not set(frappe.get_roles()) & set(BOQ_APPROVE_ROLES):
		frappe.throw(_("You are not permitted to approve a BOQ."), frappe.PermissionError)
	doc = frappe.get_doc("BOQ", boq)
	if doc.status not in ("Submitted", "Draft"):
		frappe.throw(_("Only a Draft or Submitted BOQ can be approved."))
	_validate_codes_for_approval(doc)
	for other in frappe.get_all(
		"BOQ",
		filters={"project": doc.project, "status": "Approved", "name": ("!=", doc.name)},
		pluck="name",
	):
		frappe.db.set_value("BOQ", other, "status", "Superseded")
	doc.status = "Approved"
	doc.approved_by = frappe.session.user
	doc.approved_date = today()
	doc.save(ignore_permissions=True)
	return doc.status


# --- assembly explosion ---------------------------------------------------


@frappe.whitelist()
def explode_item(boq_item: str):
	"""Replace a BOQ Item's sub-items with snapshot rows from its Assembly's
	components (qty = coefficient x driving_qty). Idempotent. Draft only."""
	item = frappe.get_doc("BOQ Item", boq_item)
	_require_write(item.boq)
	_require_draft(frappe.get_doc("BOQ", item.boq))
	if not item.assembly:
		frappe.throw(_("Link an Assembly to this item before exploding it."))
	assembly = frappe.get_doc("Assembly", item.assembly)
	driving = flt(item.driving_qty) or flt(item.planned_qty)

	# Preserve any caller's batch flag (import_template explodes many items).
	prior_skip = frappe.flags.get("boq_skip_rollup")
	frappe.flags.boq_skip_rollup = True
	try:
		for sub in frappe.get_all("BOQ Sub Item", filters={"boq_item": item.name}, pluck="name"):
			frappe.delete_doc("BOQ Sub Item", sub, force=True, ignore_permissions=True)
		for c in assembly.components:
			frappe.get_doc(
				{
					"doctype": "BOQ Sub Item",
					"boq": item.boq,
					"boq_item": item.name,
					"rate_master": c.resource,
					"description": c.resource_name or c.resource,
					"qty_per_unit": c.coefficient,
					"qty": flt(c.coefficient) * driving,
					"work_package": item.work_package,
					"cost_head": item.cost_head,
					"source_assembly": assembly.name,
				}
			).insert(ignore_permissions=True)
		item.quantity_source = "Assembly"
		item.rate = assembly.rate_per_unit
		item.planned_qty = driving
		item.driving_qty = driving
		item.save(ignore_permissions=True)
	finally:
		frappe.flags.boq_skip_rollup = prior_skip
	recompute_boq(item.boq)
	return {"item": item.name, "sub_items": len(assembly.components)}


# --- actuals --------------------------------------------------------------


@frappe.whitelist()
def recalculate_actuals(boq: str):
	"""For items linked to a Task, set actual_qty = planned_qty x task.progress%."""
	_require_write(boq)
	frappe.flags.boq_skip_rollup = True
	try:
		for it in frappe.get_all(
			"BOQ Item",
			filters={"boq": boq, "task": ("is", "set")},
			fields=["name", "task", "planned_qty", "rate"],
		):
			progress = flt(frappe.db.get_value("Task", it.task, "progress"))
			aq = flt(it.planned_qty) * progress / 100.0
			frappe.db.set_value(
				"BOQ Item",
				it.name,
				{"actual_qty": aq, "actual_amount": aq * flt(it.rate)},
				update_modified=False,
			)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(boq)
	return True


# --- clone / revisions ----------------------------------------------------


def _clone_tree(src_boq, dst_boq, reset_actuals=True, src_wp=None, wp_override=None, drop_wp=False):
	"""Clone groups/items/sub-items from src_boq into dst_boq. `src_wp` limits the
	source to items tagged to that Work Package; `wp_override` retags the copies.
	`drop_wp` clears the Work Package on the copies (used for a cross-project clone,
	where the source project's Work Packages don't belong to the target project)."""
	group_map = {}
	for g in frappe.get_all(
		"BOQ Group", filters={"boq": src_boq}, fields=["name", "code", "group_name", "idx_order"]
	):
		ng = frappe.get_doc(
			{
				"doctype": "BOQ Group",
				"boq": dst_boq,
				"code": g.code,
				"group_name": g.group_name,
				"idx_order": g.idx_order,
			}
		).insert(ignore_permissions=True)
		group_map[g.name] = ng.name

	item_filters = {"boq": src_boq}
	if src_wp:
		item_filters["work_package"] = src_wp
	item_map = {}
	count = 0
	for it in frappe.get_all("BOQ Item", filters=item_filters, fields=["*"]):
		ni = frappe.get_doc(
			{
				"doctype": "BOQ Item",
				"boq": dst_boq,
				"boq_group": group_map.get(it.boq_group),
				"code": it.code,
				"description": it.description,
				"unit": it.unit,
				"planned_qty": it.planned_qty,
				"rate": it.rate,
				"task": None if reset_actuals else it.task,
				"quantity_source": it.quantity_source,
				"work_package": wp_override or (None if drop_wp else it.work_package),
				"cost_head": it.cost_head,
				"assembly": it.assembly,
				"driving_qty": it.driving_qty,
			}
		).insert(ignore_permissions=True)
		item_map[it.name] = ni.name
		count += 1
		for si in frappe.get_all("BOQ Sub Item", filters={"boq_item": it.name}, fields=["*"]):
			frappe.get_doc(
				{
					"doctype": "BOQ Sub Item",
					"boq": dst_boq,
					"boq_item": ni.name,
					"rate_master": si.rate_master,
					"description": si.description,
					"qty_per_unit": si.qty_per_unit,
					"rate": si.rate,
					"qty": si.qty,
					"work_package": wp_override or (None if drop_wp else si.work_package),
					"cost_head": si.cost_head,
					"source_assembly": si.source_assembly,
				}
			).insert(ignore_permissions=True)
	# Drop groups that ended up empty (when filtering by a source WP).
	if src_wp:
		for gname in group_map.values():
			if not frappe.db.count("BOQ Item", {"boq_group": gname}):
				frappe.delete_doc("BOQ Group", gname, force=True, ignore_permissions=True)
	return count


@frappe.whitelist()
def create_revision(boq: str, source_sco: str | None = None, title: str | None = None):
	"""Clone a BOQ into a new Draft revision (revision = max+1, base_revision = src)."""
	if not frappe.has_permission("BOQ", "create"):
		frappe.throw(_("You are not permitted to create a BOQ."), frappe.PermissionError)
	src = frappe.get_doc("BOQ", boq)
	revs = frappe.get_all("BOQ", filters={"project": src.project}, pluck="revision")
	next_rev = (max(revs) if revs else 0) + 1
	new = frappe.get_doc(
		{
			"doctype": "BOQ",
			"project": src.project,
			"title": title or f"Revision {next_rev}",
			"revision": next_rev,
			"base_revision": src.name,
			"source_sco": source_sco,
			"status": "Draft",
			"margin_rate": src.margin_rate,
			"tax_rate": src.tax_rate,
		}
	).insert(ignore_permissions=True)
	frappe.flags.boq_skip_rollup = True
	try:
		_clone_tree(src.name, new.name)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(new.name)
	return new.name


@frappe.whitelist()
def clone_boq(from_project: str, to_project: str, from_work_package: str | None = None, to_work_package: str | None = None, title: str | None = None):
	"""project->project: a new Draft BOQ on the target. wp->wp (same project):
	retag the source WP's lines onto the project's latest BOQ."""
	if not frappe.has_permission("BOQ", "create"):
		frappe.throw(_("You are not permitted to create a BOQ."), frappe.PermissionError)
	src = frappe.get_all(
		"BOQ", filters={"project": from_project}, order_by="revision desc", limit=1, pluck="name"
	)
	if not src:
		frappe.throw(_("No BOQ found on the source project."))
	src = src[0]

	if from_project == to_project:
		if not (from_work_package and to_work_package):
			frappe.throw(_("Both source and target Work Package are required for a within-project clone."))
		frappe.flags.boq_skip_rollup = True
		try:
			count = _clone_tree(
				src, src, reset_actuals=True, src_wp=from_work_package, wp_override=to_work_package
			)
		finally:
			frappe.flags.boq_skip_rollup = False
		recompute_boq(src)
		return {"mode": "wp-to-wp", "boq": src, "cloned": count}

	new = frappe.get_doc(
		{
			"doctype": "BOQ",
			"project": to_project,
			"title": title or f"Cloned from {src}",
			"status": "Draft",
		}
	).insert(ignore_permissions=True)
	frappe.flags.boq_skip_rollup = True
	try:
		count = _clone_tree(src, new.name, wp_override=to_work_package, drop_wp=True)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(new.name)
	return {"mode": "project-to-project", "boq": new.name, "cloned": count}


# --- template import ------------------------------------------------------


@frappe.whitelist()
def import_template(boq: str, estimate_template: str):
	"""Seed groups + items from an Estimate Template into a Draft BOQ. Assembly rows
	auto-explode; Resource rows get a single rate-analysis sub-item."""
	_require_write(boq)
	boq_doc = frappe.get_doc("BOQ", boq)
	_require_draft(boq_doc)
	tpl = frappe.get_doc("Estimate Template", estimate_template)

	# Reuse existing groups by name; create lazily otherwise.
	groups = {
		g.group_name: g.name
		for g in frappe.get_all("BOQ Group", filters={"boq": boq}, fields=["name", "group_name"])
	}
	seeded = 0
	frappe.flags.boq_skip_rollup = True
	try:
		for idx, row in enumerate(tpl.rows, start=1):
			gname = row.group_name or "General"
			if gname not in groups:
				code = chr(ord("A") + len(groups)) if len(groups) < 26 else f"G{len(groups) + 1}"
				ng = frappe.get_doc(
					{
						"doctype": "BOQ Group",
						"boq": boq,
						"code": code,
						"group_name": gname,
						"idx_order": len(groups) + 1,
					}
				).insert(ignore_permissions=True)
				groups[gname] = ng.name
			item = frappe.get_doc(
				{
					"doctype": "BOQ Item",
					"boq": boq,
					"boq_group": groups[gname],
					"code": f"{idx}",
					"description": row.description or (row.assembly or row.resource or "Item"),
					"unit": row.uom or "Nos",
					"planned_qty": row.placeholder_qty or 0,
					"driving_qty": row.placeholder_qty or 0,
					"rate": row.rate or 0,
					"cost_head": row.cost_head or None,
					"quantity_source": "Template",
					"assembly": row.assembly if row.line_type == "Assembly" else None,
				}
			).insert(ignore_permissions=True)
			seeded += 1
			if row.line_type == "Assembly" and row.assembly:
				explode_item(item.name)
			elif row.line_type == "Resource" and row.resource:
				frappe.get_doc(
					{
						"doctype": "BOQ Sub Item",
						"boq": boq,
						"boq_item": item.name,
						"rate_master": row.resource,
						"description": row.description or row.resource,
						"qty_per_unit": 1,
						"work_package": item.work_package,
						"cost_head": item.cost_head,
					}
				).insert(ignore_permissions=True)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(boq)
	return {"boq": boq, "seeded": seeded}



# --- CSV / Excel file import & export -------------------------------------

_VALID_COST_HEADS = {
	"material": "Material",
	"vật tư": "Material",
	"labour": "Labour",
	"labor": "Labour",
	"nhân công": "Labour",
	"equipment": "Equipment",
	"machinery": "Equipment",
	"thiết bị": "Equipment",
	"máy móc": "Equipment",
	"máy thi công": "Equipment",
	"subcontract": "Subcontract",
	"thầu phụ": "Subcontract",
	"preliminaries": "Preliminaries",
	"chi phí chung": "Preliminaries",
	"other": "Other",
	"khác": "Other",
}

_HEADER_ALIASES = {
	"type": "type",
	"loại": "type",
	"loại dòng": "type",
	"row type": "type",
	"group": "group",
	"nhóm": "group",
	"section": "group",
	"hạng mục lớn": "group",
	"code": "code",
	"mã": "code",
	"mã hiệu": "code",
	"cost code": "code",
	"mã chi phí": "code",
	"item code": "code",
	"stt": "code",
	"description": "description",
	"mô tả": "description",
	"nội dung công việc": "description",
	"tên công việc": "description",
	"hạng mục": "description",
	"diễn giải": "description",
	"name": "description",
	"unit": "unit",
	"uom": "unit",
	"đvt": "unit",
	"đơn vị": "unit",
	"đơn vị tính": "unit",
	"planned qty": "planned_qty",
	"qty": "planned_qty",
	"quantity": "planned_qty",
	"khối lượng": "planned_qty",
	"khối lượng kế hoạch": "planned_qty",
	"số lượng": "planned_qty",
	"qty per unit": "planned_qty",
	"rate": "rate",
	"unit rate": "rate",
	"đơn giá": "rate",
	"planned amount": "planned_amount",
	"amount": "planned_amount",
	"thành tiền": "planned_amount",
	"giá trị kế hoạch": "planned_amount",
	"work package": "work_package",
	"gói công việc": "work_package",
	"wp": "work_package",
	"cost head": "cost_head",
	"loại chi phí": "cost_head",
	"khoản mục chi phí": "cost_head",
	"assembly": "assembly",
	"cụm định mức": "assembly",
	"rate master": "rate_master",
	"danh mục đơn giá": "rate_master",
	"resource": "rate_master",
}


def _read_raw_grid(file_content_b64: str, filename: str) -> list[list[str]]:
	"""Decode base64 `.csv`, `.xlsx`, or `.xls` into a 2D list of strings."""
	import base64
	import csv
	import io

	if not file_content_b64:
		frappe.throw(_("File content is empty."))
	if "," in file_content_b64 and file_content_b64.lstrip().startswith("data:"):
		file_content_b64 = file_content_b64.split(",", 1)[1]
	raw = base64.b64decode(file_content_b64)
	ext = (filename or "").lower().rsplit(".", 1)[-1]

	if ext == "xlsx":
		from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

		rows = read_xlsx_file_from_attached_file(fcontent=raw) or []
		return [["" if c is None else str(c).strip() for c in r] for r in rows]
	if ext == "xls":
		from frappe.utils.xlsxutils import read_xls_file_from_attached_file

		rows = read_xls_file_from_attached_file(raw) or []
		return [["" if c is None else str(c).strip() for c in r] for r in rows]

	# Default: CSV (try utf-8-sig, then utf-16, then cp1258/latin-1)
	text = None
	for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1258", "latin-1"):
		try:
			text = raw.decode(enc)
			break
		except UnicodeDecodeError:
			continue
	if text is None:
		text = raw.decode("utf-8", errors="replace")

	sample = text[:4096]
	delimiter = ","
	try:
		dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
		delimiter = dialect.delimiter
	except Exception:
		if sample.count(";") > sample.count(","):
			delimiter = ";"
		elif sample.count("\t") > sample.count(","):
			delimiter = "\t"

	reader = csv.reader(io.StringIO(text), delimiter=delimiter)
	return [[(c or "").strip() for c in r] for r in reader]


def _clean_num(val) -> float:
	if val is None or val == "":
		return 0.0
	if isinstance(val, (int, float)):
		return flt(val)
	s = str(val).strip().replace(" ", "")
	for sym in ("₹", "$", "€", "₫", "VND", "vnđ"):
		s = s.replace(sym, "")
	# Handle thousand separators: if both ',' and '.' exist, whichever is last is decimal
	if "," in s and "." in s:
		if s.rfind(",") > s.rfind("."):
			s = s.replace(".", "").replace(",", ".")
		else:
			s = s.replace(",", "")
	elif "," in s:
		parts = s.split(",")
		if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and parts[0].lstrip("-").isdigit()):
			s = s.replace(",", "")
		else:
			s = s.replace(",", ".")
	try:
		return flt(s)
	except Exception:
		return 0.0


def _parse_boq_grid(grid: list[list[str]]) -> dict:
	"""Parse a 2D grid of strings into structured BOQ groups -> items -> sub_items."""
	non_empty = [r for r in grid if any(c for c in r)]
	if not non_empty:
		frappe.throw(_("The uploaded file contains no rows."))

	# Locate the header row (skip optional top metadata row like 'BOQ,BOQ-0001,...')
	header_idx = None
	col_map = {}
	for idx, row in enumerate(non_empty[:15]):
		mapped = {}
		for c_idx, cell in enumerate(row):
			key = _HEADER_ALIASES.get(cell.strip().lower().rstrip("*").strip())
			if key and key.strip() not in mapped:
				mapped[key.strip()] = c_idx
		if "description" in mapped and ("code" in mapped or "type" in mapped or "planned_qty" in mapped):
			header_idx = idx
			col_map = mapped
			break

	if header_idx is None:
		first = non_empty[0]
		start_idx = 0
		if first and first[0].upper() == "BOQ":
			start_idx = 1
		if start_idx < len(non_empty) and non_empty[start_idx][0].lower() in ("type", "loại", "code", "mã"):
			start_idx += 1
		data_rows = non_empty[start_idx:]
		col_map = {
			"type": 0,
			"code": 1,
			"description": 2,
			"unit": 3,
			"planned_qty": 4,
			"rate": 5,
			"planned_amount": 6,
			"work_package": 9,
			"cost_head": 10,
		}
	else:
		data_rows = non_empty[header_idx + 1 :]

	def cell(row, key):
		i = col_map.get(key)
		if i is None or i >= len(row):
			return ""
		return (row[i] or "").strip()

	groups = []
	groups_by_key = {}
	current_group = None
	current_item = None
	item_count = 0
	sub_count = 0

	def ensure_group(code_val: str, name_val: str):
		nonlocal current_group
		key = (code_val or name_val or "General").strip()
		if key in groups_by_key:
			current_group = groups_by_key[key]
			return current_group
		g_code = (code_val or "").strip()
		if not g_code:
			idx_num = len(groups)
			g_code = chr(ord("A") + idx_num) if idx_num < 26 else f"G{idx_num + 1}"
		g_name = (name_val or code_val or _("General")).strip()
		grp = {"code": g_code, "name": g_name, "items": []}
		groups.append(grp)
		groups_by_key[key] = grp
		groups_by_key[g_code] = grp
		groups_by_key[g_name] = grp
		current_group = grp
		return grp

	for row in data_rows:
		if not any(row):
			continue
		row_type_raw = cell(row, "type").lower()
		code_val = cell(row, "code")
		desc_val = cell(row, "description")
		group_col = cell(row, "group")
		unit_val = cell(row, "unit")
		qty_raw = cell(row, "planned_qty")
		rate_raw = cell(row, "rate")
		wp_val = cell(row, "work_package")
		ch_raw = cell(row, "cost_head")
		asm_val = cell(row, "assembly")
		rm_val = cell(row, "rate_master")

		if code_val.lower() in ("code", "mã", "mã hiệu") and desc_val.lower() in (
			"description",
			"mô tả",
			"nội dung công việc",
		):
			continue
		if row_type_raw == "boq":
			continue

		is_group = False
		is_sub = False
		if row_type_raw in ("group", "nhóm", "section", "hạng mục lớn", "g"):
			is_group = True
		elif row_type_raw in ("sub", "sub-item", "sub item", "component", "hao phí", "định mức", "s") or code_val == "↳":
			is_sub = True
		elif row_type_raw in ("item", "hạng mục", "công việc", "i"):
			pass
		else:
			if (code_val or desc_val) and not unit_val and not qty_raw and not rate_raw and not asm_val:
				is_group = True

		if is_group:
			g_code = code_val
			g_name = desc_val or group_col or code_val
			if not g_code and g_name and "—" in g_name:
				parts = [p.strip() for p in g_name.split("—", 1)]
				if len(parts[0]) <= 6:
					g_code, g_name = parts[0], parts[1]
			ensure_group(g_code, g_name)
			current_item = None
			continue

		if is_sub:
			if not current_item:
				continue
			if not desc_val and not rm_val:
				continue
			current_item["sub_items"].append(
				{
					"description": desc_val or rm_val,
					"qty_per_unit": _clean_num(qty_raw) or 1.0,
					"rate": _clean_num(rate_raw),
					"rate_master": rm_val or None,
				}
			)
			sub_count += 1
			continue

		if not desc_val and not code_val and not asm_val:
			continue

		if group_col:
			ensure_group("", group_col)
		elif current_group is None:
			prefix = code_val.split(".")[0].strip() if "." in code_val else ""
			ensure_group(prefix or "A", prefix or _("General"))

		g_items = current_group["items"]
		if not code_val:
			code_val = f"{current_group['code']}.{len(g_items) + 1:02d}"

		cost_head = _VALID_COST_HEADS.get(ch_raw.lower(), ch_raw if ch_raw in _VALID_COST_HEADS.values() else None)
		item_dict = {
			"code": code_val,
			"description": desc_val or asm_val or code_val,
			"unit": unit_val or "Nos",
			"planned_qty": _clean_num(qty_raw),
			"rate": _clean_num(rate_raw),
			"work_package": wp_val or None,
			"cost_head": cost_head,
			"assembly": asm_val or None,
			"sub_items": [],
		}
		g_items.append(item_dict)
		current_item = item_dict
		item_count += 1

	if any(g["items"] for g in groups):
		groups = [g for g in groups if g["items"]]

	return {
		"groups": groups,
		"summary": {
			"groups": len(groups),
			"items": item_count,
			"sub_items": sub_count,
		},
	}


@frappe.whitelist()
def parse_boq_file(file_content_b64: str, filename: str = "boq.csv"):
	"""Preview a `.csv`, `.xlsx`, or `.xls` BOQ file before importing."""
	grid = _read_raw_grid(file_content_b64, filename)
	parsed = _parse_boq_grid(grid)
	preview_items = []
	for g in parsed["groups"]:
		for it in g["items"][:25]:
			if len(preview_items) >= 25:
				break
			preview_items.append(
				{
					"group": f"{g['code']} — {g['name']}",
					"code": it["code"],
					"description": it["description"],
					"unit": it["unit"],
					"planned_qty": it["planned_qty"],
					"rate": it["rate"],
					"sub_items": len(it["sub_items"]),
				}
			)
	return {
		"summary": parsed["summary"],
		"preview": preview_items,
	}


def _resolve_uom(unit_str: str) -> str:
	unit_str = (unit_str or "Nos").strip()
	if not unit_str:
		return "Nos"
	existing = frappe.db.get_value("UOM", {"name": unit_str}, "name")
	if existing:
		return existing
	ci = frappe.db.sql("SELECT name FROM `tabUOM` WHERE LOWER(name) = %s LIMIT 1", (unit_str.lower(),))
	if ci:
		return ci[0][0]
	try:
		doc = frappe.get_doc({"doctype": "UOM", "uom_name": unit_str, "enabled": 1}).insert(
			ignore_permissions=True
		)
		return doc.name
	except Exception:
		return "Nos"


def _resolve_work_package(wp_str: str | None, project: str) -> str | None:
	if not wp_str or not project:
		return None
	wp_str = wp_str.strip()
	for field in ("name", "code", "work_package_name"):
		match = frappe.db.get_value("Work Package", {"project": project, field: wp_str}, "name")
		if match:
			return match
	return None


def _resolve_assembly(asm_str: str | None) -> str | None:
	if not asm_str:
		return None
	asm_str = asm_str.strip()
	for field in ("name", "assembly_code", "assembly_name"):
		match = frappe.db.get_value("Assembly", {field: asm_str}, "name")
		if match:
			return match
	return None


def _resolve_rate_master(rm_str: str | None) -> str | None:
	if not rm_str:
		return None
	rm_str = rm_str.strip()
	for field in ("name", "rate_code", "rate_name"):
		match = frappe.db.get_value("Construction Rate Master", {field: rm_str}, "name")
		if match:
			return match
	return None


@frappe.whitelist()
def import_boq_file(
	boq: str,
	file_content_b64: str,
	filename: str = "boq.csv",
	replace_existing: int | bool = 0,
):
	"""Import groups, items, and sub-items from a `.csv`, `.xlsx`, or `.xls` file into a Draft BOQ."""
	_require_write(boq)
	boq_doc = frappe.get_doc("BOQ", boq)
	_require_draft(boq_doc)

	grid = _read_raw_grid(file_content_b64, filename)
	parsed = _parse_boq_grid(grid)
	if not parsed["summary"]["items"]:
		frappe.throw(_("No valid BOQ item rows were found in the uploaded file."))

	frappe.flags.boq_skip_rollup = True
	created_groups = 0
	created_items = 0
	created_subs = 0
	try:
		if int(bool(replace_existing)):
			for sub in frappe.get_all("BOQ Sub Item", filters={"boq": boq}, pluck="name"):
				frappe.delete_doc("BOQ Sub Item", sub, force=True, ignore_permissions=True)
			for it in frappe.get_all("BOQ Item", filters={"boq": boq}, pluck="name"):
				frappe.delete_doc("BOQ Item", it, force=True, ignore_permissions=True)
			for grp in frappe.get_all("BOQ Group", filters={"boq": boq}, pluck="name"):
				frappe.delete_doc("BOQ Group", grp, force=True, ignore_permissions=True)

		existing_groups = {}
		for g in frappe.get_all("BOQ Group", filters={"boq": boq}, fields=["name", "code", "group_name"]):
			if g.code:
				existing_groups[g.code.upper()] = g.name
			if g.group_name:
				existing_groups[g.group_name.lower()] = g.name

		used_item_codes = set(frappe.get_all("BOQ Item", filters={"boq": boq}, pluck="code"))

		for g_data in parsed["groups"]:
			g_code = (g_data["code"] or "").strip()
			g_name = (g_data["name"] or g_code or "General").strip()
			grp_docname = existing_groups.get(g_code.upper()) or existing_groups.get(g_name.lower())
			if not grp_docname:
				ng = frappe.get_doc(
					{
						"doctype": "BOQ Group",
						"boq": boq,
						"code": g_code or f"G{len(existing_groups) + 1}",
						"group_name": g_name,
						"idx_order": created_groups + 1,
					}
				).insert(ignore_permissions=True)
				grp_docname = ng.name
				existing_groups[ng.code.upper()] = grp_docname
				existing_groups[g_name.lower()] = grp_docname
				created_groups += 1

			for it_data in g_data["items"]:
				code = (it_data["code"] or "").strip()
				if not code or code in used_item_codes:
					seq = 1
					base = g_code or "A"
					while f"{base}.{seq:02d}" in used_item_codes:
						seq += 1
					code = f"{base}.{seq:02d}"
				used_item_codes.add(code)

				uom = _resolve_uom(it_data.get("unit"))
				wp = _resolve_work_package(it_data.get("work_package"), boq_doc.project)
				asm = _resolve_assembly(it_data.get("assembly"))
				qty = flt(it_data.get("planned_qty"))
				rate = flt(it_data.get("rate"))

				item_doc = frappe.get_doc(
					{
						"doctype": "BOQ Item",
						"boq": boq,
						"boq_group": grp_docname,
						"code": code,
						"description": it_data["description"],
						"unit": uom,
						"planned_qty": qty,
						"driving_qty": qty,
						"rate": rate,
						"work_package": wp,
						"cost_head": it_data.get("cost_head"),
						"quantity_source": "Assembly" if asm else "Manual",
						"assembly": asm,
					}
				).insert(ignore_permissions=True)
				created_items += 1

				if asm and not it_data.get("sub_items"):
					res = explode_item(item_doc.name)
					created_subs += int(res.get("sub_items") or 0)
				elif it_data.get("sub_items"):
					sub_rate_total = 0.0
					for si_data in it_data["sub_items"]:
						rm = _resolve_rate_master(si_data.get("rate_master"))
						qpu = flt(si_data.get("qty_per_unit")) or 1.0
						s_rate = flt(si_data.get("rate"))
						sub_doc = frappe.get_doc(
							{
								"doctype": "BOQ Sub Item",
								"boq": boq,
								"boq_item": item_doc.name,
								"rate_master": rm,
								"description": si_data["description"],
								"qty_per_unit": qpu,
								"rate": s_rate,
								"qty": qpu * qty,
								"work_package": wp,
								"cost_head": it_data.get("cost_head"),
							}
						).insert(ignore_permissions=True)
						sub_rate_total += flt(sub_doc.amount)
						created_subs += 1
					if sub_rate_total > 0 and not rate:
						frappe.db.set_value(
							"BOQ Item",
							item_doc.name,
							{
								"rate": sub_rate_total,
								"planned_amount": qty * sub_rate_total,
							},
							update_modified=False,
						)
	finally:
		frappe.flags.boq_skip_rollup = False

	recompute_boq(boq)
	return {
		"boq": boq,
		"groups": created_groups,
		"items": created_items,
		"sub_items": created_subs,
	}


@frappe.whitelist()
def export_boq_xlsx(boq: str):
	"""Return a base64-encoded `.xlsx` file of the full BOQ tree (Group / Item / Sub)."""
	import base64

	from frappe.utils.xlsxutils import make_xlsx

	if not frappe.has_permission("BOQ", "read", doc=boq):
		frappe.throw(_("You do not have access to {0}.").format(boq), frappe.PermissionError)

	boq_doc = frappe.get_doc("BOQ", boq)
	rows = [
		[
			"BOQ",
			boq_doc.name,
			boq_doc.title,
			f"R{boq_doc.revision}",
			boq_doc.status,
			f"Margin {flt(boq_doc.margin_rate)}%",
			f"Tax {flt(boq_doc.tax_rate)}%",
			f"Total {flt(boq_doc.total)}",
		],
		[
			"Type",
			"Code",
			"Description",
			"Unit",
			"Planned Qty",
			"Rate",
			"Planned Amount",
			"Actual Qty",
			"Actual Amount",
			"Work Package",
			"Cost Head",
		],
	]

	groups = frappe.get_all(
		"BOQ Group",
		filters={"boq": boq},
		fields=["name", "code", "group_name", "idx_order"],
		order_by="idx_order asc, creation asc",
	)
	for g in groups:
		rows.append(["Group", g.code, g.group_name, "", "", "", "", "", "", "", ""])
		items = frappe.get_all(
			"BOQ Item",
			filters={"boq": boq, "boq_group": g.name},
			fields=[
				"name",
				"code",
				"description",
				"unit",
				"planned_qty",
				"rate",
				"planned_amount",
				"actual_qty",
				"actual_amount",
				"work_package",
				"cost_head",
			],
			order_by="code asc, creation asc",
		)
		for it in items:
			rows.append(
				[
					"Item",
					it.code,
					it.description,
					it.unit,
					flt(it.planned_qty),
					flt(it.rate),
					flt(it.planned_amount),
					flt(it.actual_qty),
					flt(it.actual_amount),
					it.work_package or "",
					it.cost_head or "",
				]
			)
			subs = frappe.get_all(
				"BOQ Sub Item",
				filters={"boq_item": it.name},
				fields=["description", "uom", "qty_per_unit", "rate", "amount"],
				order_by="creation asc",
			)
			for si in subs:
				rows.append(
					[
						"Sub",
						"↳",
						si.description,
						si.uom or "",
						flt(si.qty_per_unit),
						flt(si.rate),
						flt(si.amount),
						"",
						"",
						"",
						"",
					]
				)

	xlsx_file = make_xlsx(rows, "BOQ")
	content_b64 = base64.b64encode(xlsx_file.getvalue()).decode("ascii")
	return {
		"filename": f"{boq_doc.name}.xlsx",
		"content_b64": content_b64,
	}
