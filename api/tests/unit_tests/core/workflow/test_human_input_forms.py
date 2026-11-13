from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from core.workflow.human_input_forms import (
    load_form_dispositions_by_form_id,
    load_form_tokens_by_form_id,
)
from core.workflow.human_input_policy import (
    FormDisposition,
    HumanInputSurface,
    disposition_for_surface,
)
from models.human_input import HumanInputFormRecipient, RecipientType

TABLES = (HumanInputFormRecipient,)


def _recipient(form_id: str, recipient_type: RecipientType, access_token: str) -> HumanInputFormRecipient:
    return HumanInputFormRecipient(
        form_id=form_id,
        delivery_id=str(uuid4()),
        recipient_type=recipient_type,
        recipient_payload="{}",
        access_token=access_token,
    )


def _persist_recipients(session: Session, recipients: list[HumanInputFormRecipient]) -> None:
    session.add_all(recipients)
    session.commit()


@pytest.mark.parametrize(
    ("surface", "expected_token"),
    [
        (HumanInputSurface.CONSOLE, "console-token"),
        # The management entry still returns the BACKSTAGE token for pause-details links.
        (HumanInputSurface.CONSOLE_BACKSTAGE, "backstage-token"),
        # SERVICE_API may only act on the web-app recipient.
        (HumanInputSurface.SERVICE_API, "web-token"),
    ],
)
@pytest.mark.parametrize("sqlite_session", [TABLES], indirect=True)
def test_load_form_tokens_picks_token_for_surface(surface, expected_token, sqlite_session: Session) -> None:
    _persist_recipients(
        sqlite_session,
        [
            _recipient("form-1", RecipientType.STANDALONE_WEB_APP, "web-token"),
            _recipient("form-1", RecipientType.CONSOLE, "console-token"),
            _recipient("form-1", RecipientType.BACKSTAGE, "backstage-token"),
            _recipient("form-2", RecipientType.BACKSTAGE, "decoy-token"),
        ],
    )

    assert load_form_tokens_by_form_id(["form-1"], session=sqlite_session, surface=surface) == {
        "form-1": expected_token
    }


@pytest.mark.parametrize("sqlite_session", [TABLES], indirect=True)
def test_load_form_tokens_drops_forms_without_actionable_token(sqlite_session: Session) -> None:
    _persist_recipients(
        sqlite_session,
        [
            _recipient("form-1", RecipientType.EMAIL_MEMBER, "email-token"),
            _recipient("form-1", RecipientType.CONSOLE, ""),
        ],
    )

    assert load_form_tokens_by_form_id(["form-1"], session=sqlite_session, surface=HumanInputSurface.CONSOLE) == {}


@pytest.mark.parametrize("sqlite_session", [TABLES], indirect=True)
def test_load_form_tokens_console_surface_never_falls_back_to_backstage(sqlite_session: Session) -> None:
    # Ordinary Console interaction must not substitute a BACKSTAGE token when no
    # CONSOLE recipient exists; the management entry still returns it.
    _persist_recipients(
        sqlite_session,
        [_recipient("form-1", RecipientType.BACKSTAGE, "backstage-token")],
    )

    assert load_form_tokens_by_form_id(["form-1"], session=sqlite_session, surface=HumanInputSurface.CONSOLE) == {}
    assert load_form_tokens_by_form_id(
        ["form-1"], session=sqlite_session, surface=HumanInputSurface.CONSOLE_BACKSTAGE
    ) == {"form-1": "backstage-token"}


@pytest.mark.parametrize("sqlite_session", [TABLES], indirect=True)
def test_load_form_tokens_service_api_surface_uses_web_token(sqlite_session: Session) -> None:
    _persist_recipients(
        sqlite_session,
        [
            _recipient("form-1", RecipientType.STANDALONE_WEB_APP, "web-token"),
            _recipient("form-1", RecipientType.CONSOLE, "console-token"),
            _recipient("form-1", RecipientType.BACKSTAGE, "backstage-token"),
        ],
    )

    assert load_form_tokens_by_form_id(["form-1"], session=sqlite_session, surface=HumanInputSurface.SERVICE_API) == {
        "form-1": "web-token"
    }


@pytest.mark.parametrize("sqlite_session", [TABLES], indirect=True)
def test_load_dispositions_openapi_webapp_form_is_resumable(sqlite_session: Session) -> None:
    _persist_recipients(
        sqlite_session,
        [
            _recipient("form-1", RecipientType.STANDALONE_WEB_APP, "web-token"),
            _recipient("form-1", RecipientType.BACKSTAGE, "backstage-token"),
        ],
    )

    assert load_form_dispositions_by_form_id(["form-1"], session=sqlite_session, surface=HumanInputSurface.OPENAPI) == {
        "form-1": FormDisposition(form_token="web-token", approval_channels=["console"])
    }


@pytest.mark.parametrize("sqlite_session", [TABLES], indirect=True)
def test_load_dispositions_openapi_backstage_only_form_yields_channels_not_token(sqlite_session: Session) -> None:
    _persist_recipients(
        sqlite_session,
        [_recipient("form-1", RecipientType.BACKSTAGE, "backstage-token")],
    )

    assert load_form_dispositions_by_form_id(["form-1"], session=sqlite_session, surface=HumanInputSurface.OPENAPI) == {
        "form-1": FormDisposition(form_token=None, approval_channels=["console"])
    }


# disposition_for_surface partitions recipients into a surface-actionable resume
# token plus the approval channels of the recipients the surface may NOT act on.
_WEB = (RecipientType.STANDALONE_WEB_APP, "tok_web")
_BACKSTAGE = (RecipientType.BACKSTAGE, "tok_b")
_CONSOLE = (RecipientType.CONSOLE, "tok_c")
_EMAIL_MEMBER = (RecipientType.EMAIL_MEMBER, "t1")
_EMAIL_EXTERNAL = (RecipientType.EMAIL_EXTERNAL, "t2")


@pytest.mark.parametrize(
    ("recipients", "surface", "expected"),
    [
        # Token surface acts on the web-app recipient; blocked recipients become channels.
        ([_BACKSTAGE, _WEB], HumanInputSurface.OPENAPI, FormDisposition("tok_web", ["console"])),
        ([_EMAIL_MEMBER, _EMAIL_EXTERNAL], HumanInputSurface.OPENAPI, FormDisposition(None, ["email"])),
        ([_EMAIL_MEMBER, _BACKSTAGE], HumanInputSurface.OPENAPI, FormDisposition(None, ["console", "email"])),
        # CONSOLE acts only on the console recipient; backstage is never a substitute.
        ([_CONSOLE, _WEB], HumanInputSurface.CONSOLE, FormDisposition("tok_c", ["web_app"])),
        ([_WEB], HumanInputSurface.CONSOLE, FormDisposition(None, ["web_app"])),
        ([_CONSOLE, _BACKSTAGE], HumanInputSurface.CONSOLE, FormDisposition("tok_c", ["console"])),
        ([_BACKSTAGE], HumanInputSurface.CONSOLE, FormDisposition(None, ["console"])),
        # The management entry keeps the old shared behaviour for admin links.
        ([_CONSOLE, _BACKSTAGE], HumanInputSurface.CONSOLE_BACKSTAGE, FormDisposition("tok_b", [])),
        ([_BACKSTAGE, _WEB], HumanInputSurface.CONSOLE_BACKSTAGE, FormDisposition("tok_b", ["web_app"])),
        ([_CONSOLE], HumanInputSurface.CONSOLE_BACKSTAGE, FormDisposition("tok_c", [])),
    ],
)
def test_disposition_for_surface_partitions_token_and_channels(recipients, surface, expected) -> None:
    assert disposition_for_surface(recipients, surface=surface) == expected
