from django.core.management.base import BaseCommand, CommandError

from events.batch_requests import (
    export_request_batch_template,
    import_requests_from_excel,
)


class Command(BaseCommand):
    help = (
        "Importa requests de forma masiva desde un archivo Excel "
        "(usa openpyxl)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            help="Ruta del archivo Excel (.xlsx) con los requests.",
        )
        parser.add_argument(
            "--tenant-id",
            type=int,
            help="Tenant ID donde se crearan los requests.",
        )
        parser.add_argument(
            "--user-id",
            type=int,
            help="User ID que sera asignado como created_by.",
        )
        parser.add_argument(
            "--default-timezone-id",
            type=int,
            default=None,
            help="Timezone ID por defecto para filas sin timezone_id.",
        )
        parser.add_argument(
            "--default-request-type-id",
            type=int,
            default=None,
            help="Request type ID por defecto para filas sin request_type_id.",
        )
        parser.add_argument(
            "--sheet-name",
            type=str,
            default="0",
            help="Nombre de hoja o indice (default: 0).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Valida todas las filas sin insertar en base de datos.",
        )
        parser.add_argument(
            "--template-out",
            type=str,
            help="Genera un template Excel y termina.",
        )

    def handle(self, *args, **options):
        template_out = options.get("template_out")
        if template_out:
            output_path = export_request_batch_template(template_out)
            self.stdout.write(
                self.style.SUCCESS(f"Template generado: {output_path}")
            )
            return

        file_path = options.get("file")
        tenant_id = options.get("tenant_id")
        user_id = options.get("user_id")

        if not file_path:
            raise CommandError("--file es requerido.")
        if not tenant_id:
            raise CommandError("--tenant-id es requerido.")
        if not user_id:
            raise CommandError("--user-id es requerido.")

        sheet_name_raw = options.get("sheet_name")
        sheet_name: str | int = sheet_name_raw
        if isinstance(sheet_name_raw, str) and sheet_name_raw.isdigit():
            sheet_name = int(sheet_name_raw)

        try:
            result = import_requests_from_excel(
                file_path=file_path,
                tenant_id=tenant_id,
                created_by_id=user_id,
                default_timezone_id=options.get("default_timezone_id"),
                default_request_type_id=options.get("default_request_type_id"),
                sheet_name=sheet_name,
                dry_run=options.get("dry_run", False),
            )
        except Exception as exc:
            raise CommandError(str(exc))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Resumen importacion batch"))
        self.stdout.write(f"Total filas: {result.total_rows}")
        self.stdout.write(f"Exitosas: {result.success_count}")
        self.stdout.write(f"Fallidas: {result.failed_count}")
        self.stdout.write("")

        if result.failed_count:
            self.stdout.write(self.style.WARNING("Filas con error:"))
            for row in result.rows:
                if row.success:
                    continue
                self.stdout.write(
                    self.style.WARNING(f" - fila {row.row_number}: {row.message}")
                )

        if result.success_count:
            self.stdout.write(self.style.SUCCESS("Filas importadas:"))
            for row in result.rows:
                if not row.success:
                    continue
                request_label = (
                    f"request_id={row.request_id}, request_uuid={row.request_uuid}"
                    if row.request_id
                    else "validada (dry-run)"
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f" - fila {row.row_number}: {row.message} ({request_label})"
                    )
                )
