# Django MySQL Schema Sync

A lightweight schema synchronization tool that reconciles Django ORM models with a live MySQL database **without relying on traditional migrations**.

Built to address production challenges where running Django migrations on large tables can cause **long locks, downtime, and deployment risk**.

---

## 🚀 Why this exists

Django migrations work well in most cases, but in production environments with large datasets, they can become problematic:

- Full table locks during `ALTER TABLE`
- Long-running migrations on large tables
- Risky deployments with no preview of changes
- Schema drift between environments

This tool provides a **controlled, incremental alternative**.

---

## ✨ Features

- 🔍 **Schema Diff Engine**
  - Compares Django models with live DB schema using `INFORMATION_SCHEMA`

- 🛠 **Incremental DDL Execution**
  - Applies only required changes:
    - `ADD COLUMN`
    - `MODIFY COLUMN`
    - `DROP COLUMN`

- 🧠 **Generated Column Support**
  - Handles Django `GeneratedField` with correct SQL expressions

- 🔗 **Foreign Key Handling**
  - Safely drops constraints before modifying dependent columns

- 📚 **Index Synchronization**
  - Detects and reconciles:
    - `db_index`
    - `unique`
    - `Meta.indexes`
    - `unique_together`

- 🧪 **Dry Run Mode**
  - Preview SQL before execution

---

## ⚙️ How it works

1. Introspects database schema using:
   - `INFORMATION_SCHEMA.COLUMNS`
   - `SHOW INDEX`
   - `KEY_COLUMN_USAGE`

2. Compares with Django model definitions

3. Generates minimal SQL diff

4. Executes safe `ALTER TABLE` statements

---

## 🧑‍💻 Usage

```bash
python sync_schema.py
