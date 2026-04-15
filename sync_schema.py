import os
import re
import sys
import django
from decimal import Decimal
from django.apps import apps
from django.db import connection, models
from django.db.models import GeneratedField
from django.db.backends.utils import CursorWrapper

# Initialize with your project's settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project_name.settings")
django.setup()

DRY_RUN = False


def run_ddl(cursor, sql):
    if DRY_RUN:
        print(sql)
    else:
        cursor.execute(sql)


def get_mysql_base_type(field: models.Field) -> str:
    internal = field.get_internal_type()
    if internal == "AutoField":
        return "INT AUTO_INCREMENT"
    if internal == "BigAutoField":
        return "BIGINT AUTO_INCREMENT"
    if internal == "SmallAutoField":
        return "SMALLINT AUTO_INCREMENT"
    if internal in (
        "CharField", "SlugField", "URLField", "EmailField",
        "FilePathField", "FileField", "ImageField",
    ):
        max_length = getattr(field, "max_length", None) or 255
        return f"VARCHAR({max_length})"
    if internal == "TextField":
        return "TEXT"
    if internal == "IntegerField":
        return "INT"
    if internal == "BigIntegerField":
        return "BIGINT"
    if internal == "SmallIntegerField":
        return "SMALLINT"
    if internal == "PositiveIntegerField":
        return "INT UNSIGNED"
    if internal == "PositiveSmallIntegerField":
        return "SMALLINT UNSIGNED"
    if internal == "PositiveBigIntegerField":
        return "BIGINT UNSIGNED"
    if internal == "FloatField":
        return "FLOAT"
    if internal == "DecimalField":
        max_digits = getattr(field, "max_digits", 10) or 10
        decimal_places = getattr(field, "decimal_places", 2) or 2
        return f"DECIMAL({max_digits},{decimal_places})"
    if internal in ("BooleanField", "NullBooleanField"):
        return "TINYINT(1)"
    if internal == "DateTimeField":
        return "DATETIME"
    if internal == "DateField":
        return "DATE"
    if internal == "TimeField":
        return "TIME"
    if internal == "DurationField":
        return "BIGINT"
    if internal == "UUIDField":
        return "CHAR(32)"
    if internal == "BinaryField":
        return "LONGBLOB"
    if internal in ("IPAddressField", "GenericIPAddressField"):
        return "CHAR(39)"
    if internal == "JSONField":
        return "JSON"
    if internal in ("ForeignKey", "OneToOneField"):
        related_pk = field.related_model._meta.pk
        if related_pk:
            rtype = related_pk.get_internal_type()
            if rtype in ("BigAutoField", "BigIntegerField"):
                return "BIGINT"
            if rtype == "UUIDField":
                return "CHAR(32)"
            if rtype in ("SmallAutoField", "SmallIntegerField"):
                return "SMALLINT"
        return "INT"
    return "TEXT"


def get_mysql_type(field: models.Field) -> str:
    mysql_type = get_mysql_base_type(field)
    null_sql = "NULL" if field.null else "NOT NULL"
    default_sql = ""
    if field.has_default():
        default_val = field.default
        if callable(default_val):
            default_val = default_val()
        if isinstance(default_val, bool):
            default_sql = f"DEFAULT {int(default_val)}"
        elif default_val is None:
            default_sql = "DEFAULT NULL"
        elif isinstance(default_val, str):
            escaped = default_val.replace("'", "''")
            default_sql = f"DEFAULT '{escaped}'"
        elif isinstance(default_val, (int, float, Decimal)):
            default_sql = f"DEFAULT {default_val}"
        # dict, list, and other complex types: skip DB-level default
    return f"{mysql_type} {null_sql} {default_sql}"


_INT_WIDTH_RE = re.compile(
    r'\b(TINYINT|SMALLINT|MEDIUMINT|INT|BIGINT)\(\d+\)',
)


def normalize_db_type(col_type: str) -> str:
    """Normalize MySQL COLUMN_TYPE for comparison.
    Strips integer display widths (int(11) -> INT) except
    tinyint(1) which maps to BooleanField.
    """
    t = col_type.upper().strip()
    if t == "TINYINT(1)":
        return t
    return _INT_WIDTH_RE.sub(lambda m: m.group(1), t)


def get_db_column_details(cursor, table_name):
    cursor.execute("""
        SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, GENERATION_EXPRESSION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s;
    """, [table_name])
    result = {}
    for col_name, col_type, is_nullable, gen_expr in cursor.fetchall():
        if gen_expr:
            continue
        result[col_name] = {
            "type": normalize_db_type(col_type),
            "nullable": is_nullable == "YES",
        }
    return result


def get_existing_tables():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE();"
        )
        return {row[0] for row in cursor.fetchall()}


def get_generated_column_sql(field, model):
    output_field = field.output_field
    mysql_type = get_mysql_base_type(output_field)
    compiler = connection.ops.compiler("SQLCompiler")(
        field.expression.resolve_expression(
            model.objects.all().query, allow_joins=False
        ).query if hasattr(field.expression, 'resolve_expression')
        else model.objects.all().query,
        connection, None
    )
    expr_sql, params = field.expression.resolve_expression(
        model.objects.all().query, allow_joins=False,
    ).as_sql(compiler, connection)
    if params:
        expr_sql = expr_sql % tuple(
            connection.ops.adapt_decimalfield_value(p)
            if isinstance(p, float) else
            f"'{p}'" if isinstance(p, str) else p
            for p in params
        )
    persist = "STORED" if field.db_persist else "VIRTUAL"
    null_sql = "NULL" if output_field.null else "NOT NULL"
    return f"{mysql_type} GENERATED ALWAYS AS ({expr_sql}) {persist} {null_sql}"


def create_table(model):
    table_name = model._meta.db_table
    col_defs = []
    pk_col = None
    for field in model._meta.fields:
        col = field.column
        if isinstance(field, GeneratedField):
            col_sql = get_generated_column_sql(field, model)
            col_defs.append(f"  `{col}` {col_sql}")
            continue
        if field.primary_key:
            pk_col = col
            mysql_type = get_mysql_base_type(field)
            col_defs.append(f"  `{col}` {mysql_type} NOT NULL")
        else:
            col_defs.append(f"  `{col}` {get_mysql_type(field)}")
    if pk_col:
        col_defs.append(f"  PRIMARY KEY (`{pk_col}`)")
    cols_sql = ",\n".join(col_defs)
    sql = f"CREATE TABLE `{table_name}` (\n{cols_sql}\n);"
    with connection.cursor() as cursor:
        run_ddl(cursor, sql)


def get_generated_columns(cursor: CursorWrapper, table_name):
    cursor.execute("""
        SELECT COLUMN_NAME, GENERATION_EXPRESSION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND GENERATION_EXPRESSION IS NOT NULL
          AND GENERATION_EXPRESSION != '';
    """, [table_name])
    return {row[0]: row[1] for row in cursor.fetchall()}


def sync_model_fields(model: models.Model):
    table_name = model._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}`;")
        db_columns = {row[0] for row in cursor.fetchall()}

        cursor.execute(
            "SELECT CONSTRAINT_NAME, COLUMN_NAME "
            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "  AND TABLE_NAME = %s "
            "  AND REFERENCED_TABLE_NAME IS NOT NULL;",
            [table_name],
        )
        fk_map = {row[1]: row[0] for row in cursor.fetchall()}
        generated_columns = get_generated_columns(cursor, table_name)
        db_details = get_db_column_details(cursor, table_name)

        model_fields = {field.column for field in model._meta.fields}

        extra_columns = [col for col in db_columns if col not in model_fields]

        missing_fields = [
            field for field in model._meta.fields
            if field.column not in db_columns and not field.primary_key
        ]

        # Detect columns needing type/nullable modification
        modify_clauses = []
        modify_cols = set()
        for field in model._meta.fields:
            if field.primary_key or isinstance(field, GeneratedField):
                continue
            col = field.column
            if col not in db_details:
                continue
            db_info = db_details[col]
            expected_type = normalize_db_type(get_mysql_base_type(field))
            if expected_type != db_info["type"] or field.null != db_info["nullable"]:
                modify_clauses.append(
                    f"MODIFY COLUMN `{col}` {get_mysql_type(field)}"
                )
                modify_cols.add(col)

        # FK constraints that must be dropped before column changes
        fk_drop_clauses = []
        for col in set(extra_columns) | modify_cols:
            if col in fk_map:
                fk_drop_clauses.append(
                    f"DROP FOREIGN KEY `{fk_map[col]}`"
                )

        drop_clauses = []
        for col in extra_columns:
            if col in generated_columns:
                continue
            dependents = [
                gc for gc, expr in generated_columns.items()
                if col in expr
            ]
            if dependents:
                continue
            drop_clauses.append(f"DROP COLUMN `{col}`")

        add_clauses = []
        for field in missing_fields:
            if isinstance(field, GeneratedField):
                col_sql = get_generated_column_sql(field, model)
            else:
                col_sql = get_mysql_type(field)
            add_clauses.append(f"ADD COLUMN `{field.column}` {col_sql}")

        all_clauses = fk_drop_clauses + drop_clauses + add_clauses + modify_clauses
        if all_clauses:
            run_ddl(
                cursor,
                f"ALTER TABLE `{table_name}` {', '.join(all_clauses)};",
            )


def get_db_indexes(cursor, table_name):
    cursor.execute(f"SHOW INDEX FROM `{table_name}`;")
    indexes = {}
    for row in cursor.fetchall():
        idx_name = row[2]
        non_unique = row[1]
        col_name = row[4]
        if idx_name not in indexes:
            indexes[idx_name] = {"unique": not non_unique, "columns": []}
        indexes[idx_name]["columns"].append(col_name)
    return indexes


def get_fk_index_names(cursor, table_name):
    cursor.execute("""
        SELECT CONSTRAINT_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND CONSTRAINT_TYPE = 'FOREIGN KEY';
    """, [table_name])
    return {row[0] for row in cursor.fetchall()}


def get_expected_indexes(model):
    table_name = model._meta.db_table
    expected = {}

    # Field-level db_index / unique
    for field in model._meta.fields:
        if field.primary_key:
            continue
        col = field.column
        if field.db_index and not field.unique:
            # Django's default naming: <table>_<col>_<hash>
            idx_name = f"{table_name}_{col}_idx"
            expected[idx_name] = {"unique": False, "columns": [col]}
        if field.unique:
            idx_name = f"{table_name}_{col}_uniq"
            expected[idx_name] = {"unique": True, "columns": [col]}

    # Meta.indexes
    for index in model._meta.indexes:
        cols = [
            model._meta.get_field(field_name.lstrip("-")).column
            for field_name in index.fields
        ]
        idx_name = index.name or f"{table_name}_{'_'.join(cols)}_idx"
        expected[idx_name] = {"unique": False, "columns": cols}

    # Meta.unique_together
    for fields in model._meta.unique_together:
        cols = [model._meta.get_field(f).column for f in fields]
        idx_name = f"{table_name}_{'_'.join(cols)}_uniq"
        expected[idx_name] = {"unique": True, "columns": cols}

    return expected


def sync_indexes(model):
    table_name = model._meta.db_table
    with connection.cursor() as cursor:
        db_indexes = get_db_indexes(cursor, table_name)
        fk_index_names = get_fk_index_names(cursor, table_name)

        expected = get_expected_indexes(model)
        db_by_cols = {
            (tuple(info["columns"]), info["unique"]): name
            for name, info in db_indexes.items()
        }

        expected_by_cols = {
            (tuple(info["columns"]), info["unique"]): name
            for name, info in expected.items()
        }
        alter_clauses = []

        for key, db_name in db_by_cols.items():
            if db_name == "PRIMARY" or db_name in fk_index_names:
                continue
            if key not in expected_by_cols:
                alter_clauses.append(f"DROP INDEX `{db_name}`")

        for key in expected_by_cols:
            if key not in db_by_cols:
                cols, unique = key
                exp_name = expected_by_cols[key]
                col_sql = ", ".join(f"`{c}`" for c in cols)
                if unique:
                    alter_clauses.append(
                        f"ADD UNIQUE INDEX `{exp_name}` ({col_sql})"
                    )
                else:
                    alter_clauses.append(
                        f"ADD INDEX `{exp_name}` ({col_sql})"
                    )

        if alter_clauses:
            run_ddl(
                cursor,
                f"ALTER TABLE `{table_name}` {', '.join(alter_clauses)};",
            )


def main():
    global DRY_RUN
    DRY_RUN = "--dry-run" in sys.argv
    sync_idx = "--sync-indexes" in sys.argv
    # include_auto_created=True picks up M2M intermediate tables
    all_models = apps.get_models(include_auto_created=True)
    """
    Do not attempt to sync models from these apps
    as they have been altered manually or have complex constraints/indexes
    that this script doesn't handle.
    """
    skip_models = ['wagtail', 'cms', 'socialaccount']
    existing_tables = get_existing_tables()
    for model in all_models:
        if model._meta.proxy or not model._meta.managed:
            continue
        if any(model._meta.app_label.startswith(key) for key in skip_models):
            continue
        table_name = model._meta.db_table
        if table_name not in existing_tables:
            create_table(model)
        else:
            sync_model_fields(model)
        if sync_idx:
            sync_indexes(model)


if __name__ == "__main__":
    main()

