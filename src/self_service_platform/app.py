from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.cli.main import create_app
from self_service_platform.wiring import build_service, open_registry

app = create_app(
    build_catalog(),
    open_registry=open_registry,
    open_service=build_service,
)
