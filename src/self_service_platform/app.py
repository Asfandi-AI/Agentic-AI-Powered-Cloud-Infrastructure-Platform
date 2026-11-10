import typer

from self_service_platform import wiring
from self_service_platform.catalog.catalog import build_catalog
from self_service_platform.cli.main import create_app
from self_service_platform.core.chat_host import ChatHost
from self_service_platform.core.fake_model_client import FakeModelClient
from self_service_platform.core.service import PlatformService
from self_service_platform.core.tenant import Initiator


def _chat(service: PlatformService, host: ChatHost, initiator: Initiator) -> None:
    client = wiring.build_model_client()
    if isinstance(client, FakeModelClient):
        typer.echo("no model client is configured", err=True)
        raise typer.Exit(2)
    wiring.run_configured_chat(service, client, initiator, host)


app = create_app(
    build_catalog(),
    open_registry=wiring.open_registry,
    open_service=wiring.build_service,
    chat_runner=_chat,
)
