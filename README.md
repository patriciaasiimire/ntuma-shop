# JuiceFront 🍹

**Fresh Juice Delivered** — An Airbnb-style marketplace connecting customers in Nansana, Uganda with local juice vendors.

- We handle delivery and charge a small service fee (default 300 UGX).
- Vendors keep their own prices.
- Simple, mobile-first, three-click ordering.

## Tech Stack
- Python 3.10+ · Flask · SQLite
- Vanilla HTML / CSS / JavaScript

## Project Structure
```
juicefront/
├── app.py
├── requirements.txt
├── Procfile
├── .env.example
├── README.md
├── static/
│   ├── css/style.css
│   ├── js/script.js
│   └── uploads/vendors/
└── templates/
    ├── _base.html
    ├── index.html
    ├── vendor_detail.html
    ├── order_form.html
    ├── success.html
    ├── login.html
    ├── vendor_dashboard.html
    ├── operator_dashboard.html
    ├── orders.html
    └── error.html
```

## Local Setup

```bash
python -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then edit passwords
python app.py
```

Open <http://127.0.0.1:5000>.

The database (`juicefront.db`) and default users are auto-created on first run.

## Default Accounts (change in production!)
| Role      | Username           | Password (from .env)         |
| --------- | ------------------ | ---------------------------- |
| Operator  | `operator`         | `OPERATOR_PASSWORD`          |
| Vendors   | `vendor1`…`vendor6`| `VENDOR_DEFAULT_PASSWORD`    |

## Roles (RBAC)
- **Public / Customer** — Browse vendors, order juice. No login.
- **Vendor** — Login → manage profile (name, description, photo upload), manage juices, view own orders.
- **Operator (Admin)** — Full access: all orders, update statuses, daily revenue summary.

## Security
- Passwords hashed with Werkzeug.
- Session-based auth, `SESSION_SECRET` from env.
- 3-try IP lockout on `/login` (15 min).
- File uploads: images only, 2 MB max.
- Set `DEBUG=False` in production.

## Deploy to Render / Heroku
1. Push to GitHub.
2. Create a new Web Service on [Render](https://render.com).
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app` (from `Procfile`).
5. Set environment variables from `.env.example`.
6. For persistent vendor uploads, attach a Render **Persistent Disk** mounted at `/opt/render/project/src/static/uploads/vendors`. For scale, switch storage to S3 / Cloud Storage and DB to Postgres.

## License
MIT — free to use, modify, and share.
