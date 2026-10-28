"""Provision BuildSuite department logins.

Usage (from the frappe_docker folder):
  Get-Content provision_users.py -Raw | docker exec -w /home/frappe/frappe-bench/sites -i frappe_docker-backend-1 /home/frappe/frappe-bench/env/bin/python -

Edit DOMAIN and DEPARTMENT_USERS below (or add real employees), then re-run: it is idempotent
(existing users are updated, never duplicated; passwords are only set for NEW users).
Generated passwords are printed once - copy them and make each user change theirs at first login.
"""
import secrets
import string

import frappe

DOMAIN = "buildsuite.local"  # <-- change to your company mail domain

# (login prefix, full name, department, persona)  persona must match a Persona record name.
DEPARTMENT_USERS = [
    ("director",    "Ban Giám đốc",              "Ban điều hành",        "Director / Owner"),
    ("pm",          "Quản lý dự án",             "Quản lý dự án",        "Project Manager"),
    ("estimator",   "Kỹ sư dự toán",             "Dự toán - Đấu thầu",   "Estimator"),
    ("qs",          "Kỹ sư QS",                  "Dự toán - Đấu thầu",   "Quantity Surveyor"),
    ("siteengineer","Kỹ sư hiện trường",         "Thi công",             "Site Engineer"),
    ("foreman",     "Đội trưởng / Giám sát",     "Thi công",             "Foreman / Supervisor"),
    ("procurement", "Chuyên viên mua hàng",      "Mua sắm - Cung ứng",   "Procurement Officer"),
    ("storekeeper", "Thủ kho",                   "Kho vật tư",           "Store Keeper"),
    ("accountant",  "Kế toán",                   "Tài chính - Kế toán",  "Accountant"),
    ("hr",          "Trưởng phòng Nhân sự",      "Hành chính - Nhân sự", "HR Manager"),
]


def _password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10)) + "!7a"


def main():
    frappe.init(site="frontend", sites_path=".")
    frappe.connect()
    frappe.set_user("Administrator")
    rows = []
    for prefix, full_name, dept, persona in DEPARTMENT_USERS:
        if not frappe.db.exists("Persona", persona):
            print(f"SKIP {prefix}: persona '{persona}' not found")
            continue
        email = f"{prefix}@{DOMAIN}"
        if frappe.db.exists("User", email):
            doc = frappe.get_doc("User", email)
            doc.persona = persona
            doc.flags.ignore_permissions = True
            doc.save()
            rows.append((dept, persona, email, "(đã tồn tại - giữ mật khẩu cũ)"))
            continue
        pwd = _password()
        doc = frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": full_name,
            "enabled": 1,
            "send_welcome_email": 0,
            "user_type": "System User",
            "language": "vi",
            "persona": persona,
            "new_password": pwd,
        })
        doc.flags.ignore_permissions = True
        doc.insert()
        rows.append((dept, persona, email, pwd))
    frappe.db.commit()
    print("\n{:<24}{:<26}{:<36}{}".format("PHÒNG BAN", "PERSONA", "ĐĂNG NHẬP", "MẬT KHẨU"))
    for r in rows:
        print("{:<24}{:<26}{:<36}{}".format(*r))
    for email in [r[2] for r in rows]:
        u = frappe.get_doc("User", email)
        print(email, "->", sorted(r.role for r in u.roles))


main()
