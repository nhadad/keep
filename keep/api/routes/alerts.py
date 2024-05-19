# TODO: this whole file needs to get refactored
# mainly: pusher stuff, enrichment stuff and async stuff
import copy
import datetime
import json
import logging
import os

import celpy
import dateutil.parser
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pusher import Pusher
from sqlmodel import Session

from keep.api.alert_deduplicator.alert_deduplicator import AlertDeduplicator
from keep.api.bl.enrichments import EnrichmentsBl
from keep.api.core.config import config
from keep.api.core.db import enrich_alert as enrich_alert_db
from keep.api.core.db import (
    get_alerts_by_fingerprint,
    get_alerts_with_filters,
    get_all_presets,
    get_enrichment,
    get_last_alerts,
    get_session,
)
from keep.api.core.dependencies import (
    AuthenticatedEntity,
    AuthVerifier,
    get_pusher_client,
)
from keep.api.models.alert import (
    AlertDto,
    AlertStatus,
    DeleteRequestBody,
    EnrichAlertRequestBody,
    SearchAlertsRequest,
)
from keep.api.models.db.alert import Alert, AlertRaw
from keep.api.models.db.preset import PresetDto
from keep.api.utils.email_utils import EmailTemplates, send_email
from keep.api.utils.enrichment_helpers import parse_and_enrich_deleted_and_assignees
from keep.contextmanager.contextmanager import ContextManager
from keep.providers.providers_factory import ProvidersFactory
from keep.rulesengine.rulesengine import RulesEngine
from keep.workflowmanager.workflowmanager import WorkflowManager

router = APIRouter()
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def convert_db_alerts_to_dto_alerts(alerts: list[Alert]) -> list[AlertDto]:
    """
    Enriches the alerts with the enrichment data.

    Args:
        alerts (list[Alert]): The alerts to enrich.

    Returns:
        list[AlertDto]: The enriched alerts.
    """
    alerts_dto = []
    with tracer.start_as_current_span("alerts_enrichment"):
        # enrich the alerts with the enrichment data
        for alert in alerts:
            if alert.alert_enrichment:
                alert.event.update(alert.alert_enrichment.enrichments)
            try:
                alert_dto = AlertDto(**alert.event)
                if alert.alert_enrichment:
                    parse_and_enrich_deleted_and_assignees(
                        alert_dto, alert.alert_enrichment.enrichments
                    )
            except Exception:
                # should never happen but just in case
                logger.exception(
                    "Failed to parse alert",
                    extra={
                        "alert": alert,
                    },
                )
                continue
            # enrich provider id when it's possible
            if alert_dto.providerId is None:
                alert_dto.providerId = alert.provider_id
            alerts_dto.append(alert_dto)
    return alerts_dto


def pull_alerts_from_providers(
    tenant_id: str, pusher_client: Pusher | None, sync: bool = False
) -> list[AlertDto]:
    """
    Pulls alerts from the installed providers.
    tb: THIS FUNCTION NEEDS TO BE REFACTORED!

    Args:
        tenant_id (str): The tenant id.
        pusher_client (Pusher | None): The pusher client.
        sync (bool, optional): Whether the process is sync or not. Defaults to False.

    Raises:
        HTTPException: If the pusher client is None and the process is not sync.

    Returns:
        list[AlertDto]: The pulled alerts.
    """
    if pusher_client is None and sync is False:
        raise HTTPException(500, "Cannot pull alerts async when pusher is disabled.")

    context_manager = ContextManager(
        tenant_id=tenant_id,
        workflow_id=None,
    )

    logger.info(
        f"{'Asynchronously' if sync is False else 'Synchronously'} pulling alerts from installed providers"
    )

    sync_alerts = []  # if we're running in sync mode
    for provider in ProvidersFactory.get_installed_providers(tenant_id=tenant_id):
        provider_class = ProvidersFactory.get_provider(
            context_manager=context_manager,
            provider_id=provider.id,
            provider_type=provider.type,
            provider_config=provider.details,
        )
        try:
            logger.info(
                f"Pulling alerts from provider {provider.type} ({provider.id})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                },
            )
            sorted_provider_alerts_by_fingerprint = (
                provider_class.get_alerts_by_fingerprint(tenant_id=tenant_id)
            )
            logger.info(
                f"Pulled alerts from provider {provider.type} ({provider.id})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                    "number_of_fingerprints": len(
                        sorted_provider_alerts_by_fingerprint.keys()
                    ),
                },
            )

            if sorted_provider_alerts_by_fingerprint:
                last_alerts = [
                    alerts[0]
                    for alerts in sorted_provider_alerts_by_fingerprint.values()
                ]
                if sync:
                    sync_alerts.extend(last_alerts)
                    logger.info(
                        f"Pulled alerts from provider {provider.type} ({provider.id}) (alerts: {len(sorted_provider_alerts_by_fingerprint)})",
                        extra={
                            "provider_type": provider.type,
                            "provider_id": provider.id,
                            "tenant_id": tenant_id,
                        },
                    )
                    continue

                logger.info("Batch sending pulled alerts via pusher")
                batch_send = []
                previous_compressed_batch = ""
                new_compressed_batch = ""
                number_of_alerts_in_batch = 0
                # tb: this might be too slow in the future and we might need to refactor
                for alert in last_alerts:
                    alert_dict = alert.dict()
                    batch_send.append(alert_dict)
                    new_compressed_batch = json.dumps(batch_send)
                    if len(new_compressed_batch) <= 10240:
                        number_of_alerts_in_batch += 1
                        previous_compressed_batch = new_compressed_batch
                    elif pusher_client:
                        pusher_client.trigger(
                            f"private-{tenant_id}",
                            "async-alerts",
                            previous_compressed_batch,
                        )
                        batch_send = [alert_dict]
                        new_compressed_batch = ""
                        number_of_alerts_in_batch = 1

                # this means we didn't get to this ^ else statement and loop ended
                #   so we need to send the rest of the alerts
                if (
                    new_compressed_batch
                    and len(new_compressed_batch) < 10240
                    and pusher_client
                ):
                    pusher_client.trigger(
                        f"private-{tenant_id}",
                        "async-alerts",
                        new_compressed_batch,
                    )
                logger.info("Sent batch of pulled alerts via pusher")
                # Also update the presets
                try:
                    presets = get_all_presets(tenant_id)
                    presets_do_update = []
                    for preset in presets:
                        # filter the alerts based on the search query
                        preset_dto = PresetDto(**preset.dict())
                        filtered_alerts = RulesEngine.filter_alerts(
                            last_alerts, preset_dto.cel_query
                        )
                        # if not related alerts, no need to update
                        if not filtered_alerts:
                            continue
                        presets_do_update.append(preset_dto)
                        preset_dto.alerts_count = len(filtered_alerts)
                        # update noisy
                        if preset.is_noisy:
                            firing_filtered_alerts = list(
                                filter(
                                    lambda alert: alert.status
                                    == AlertStatus.FIRING.value,
                                    filtered_alerts,
                                )
                            )
                            # if there are firing alerts, then do noise
                            if firing_filtered_alerts:
                                logger.info("Noisy preset is noisy")
                                preset_dto.should_do_noise_now = True
                        # else if at least one of the alerts has .isNoisy
                        elif any(
                            alert.isNoisy and alert.status == AlertStatus.FIRING.value
                            for alert in filtered_alerts
                            if hasattr(alert, "isNoisy")
                        ):
                            logger.info("Noisy preset is noisy")
                            preset_dto.should_do_noise_now = True
                    # send with pusher
                    if pusher_client:
                        try:
                            pusher_client.trigger(
                                f"private-{tenant_id}",
                                "async-presets",
                                json.dumps(
                                    [p.dict() for p in presets_do_update], default=str
                                ),
                            )
                        except Exception:
                            logger.exception("Failed to send presets via pusher")
                except Exception:
                    logger.exception(
                        "Failed to send presets via pusher",
                        extra={
                            "provider_type": provider.type,
                            "provider_id": provider.id,
                            "tenant_id": tenant_id,
                        },
                    )
            logger.info(
                f"Pulled alerts from provider {provider.type} ({provider.id}) (alerts: {len(sorted_provider_alerts_by_fingerprint)})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                },
            )
        except Exception as e:
            logger.warning(
                f"Could not fetch alerts from provider due to {e}",
                extra={
                    "provider_id": provider.id,
                    "provider_type": provider.type,
                    "tenant_id": tenant_id,
                },
            )
            pass
    if sync is False and pusher_client:
        pusher_client.trigger(f"private-{tenant_id}", "async-done", {})
    logger.info("Fetched alerts from installed providers")
    return sync_alerts


@router.get(
    "",
    description="Get last alerts occurrence",
)
def get_all_alerts(
    background_tasks: BackgroundTasks,
    sync: bool = False,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
    pusher_client: Pusher | None = Depends(get_pusher_client),
) -> list[AlertDto]:
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        "Fetching alerts from DB",
        extra={
            "tenant_id": tenant_id,
        },
    )
    db_alerts = get_last_alerts(tenant_id=tenant_id, limit=10000)
    enriched_alerts_dto = convert_db_alerts_to_dto_alerts(db_alerts)
    logger.info(
        "Fetched alerts from DB",
        extra={
            "tenant_id": tenant_id,
        },
    )

    if sync:
        enriched_alerts_dto.extend(
            pull_alerts_from_providers(tenant_id, pusher_client, sync=True)
        )
    else:
        logger.info("Adding task to async fetch alerts from providers")
        background_tasks.add_task(pull_alerts_from_providers, tenant_id, pusher_client)
        logger.info("Added task to async fetch alerts from providers")

    return enriched_alerts_dto


@router.get("/{fingerprint}/history", description="Get alert history")
def get_alert_history(
    fingerprint: str,
    provider_id: str | None = None,
    provider_type: str | None = None,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
) -> list[AlertDto]:
    logger.info(
        "Fetching alert history",
        extra={
            "fingerprint": fingerprint,
            "tenant_id": authenticated_entity.tenant_id,
        },
    )
    db_alerts = get_alerts_by_fingerprint(
        tenant_id=authenticated_entity.tenant_id, fingerprint=fingerprint, limit=1000
    )
    enriched_alerts_dto = convert_db_alerts_to_dto_alerts(db_alerts)

    if provider_id is not None and provider_type is not None:
        try:
            installed_provider = ProvidersFactory.get_installed_provider(
                tenant_id=authenticated_entity.tenant_id,
                provider_id=provider_id,
                provider_type=provider_type,
            )
            pulled_alerts_history = installed_provider.get_alerts_by_fingerprint(
                tenant_id=authenticated_entity.tenant_id
            ).get(fingerprint, [])
            enriched_alerts_dto.extend(pulled_alerts_history)
        except Exception:
            logger.warning(
                "Failed to pull alerts history from installed provider",
                extra={
                    "provider_id": provider_id,
                    "provider_type": provider_type,
                    "tenant_id": authenticated_entity.tenant_id,
                },
            )

    logger.info(
        "Fetched alert history",
        extra={
            "tenant_id": authenticated_entity.tenant_id,
            "fingerprint": fingerprint,
        },
    )
    return enriched_alerts_dto


@router.delete("", description="Delete alert by finerprint and last received time")
def delete_alert(
    delete_alert: DeleteRequestBody,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["delete:alert"])),
) -> dict[str, str]:
    tenant_id = authenticated_entity.tenant_id
    user_email = authenticated_entity.email

    logger.info(
        "Deleting alert",
        extra={
            "fingerprint": delete_alert.fingerprint,
            "restore": delete_alert.restore,
            "lastReceived": delete_alert.lastReceived,
            "tenant_id": tenant_id,
        },
    )

    deleted_last_received = []  # the last received(s) that are deleted
    assignees_last_receievd = {}  # the last received(s) that are assigned to someone

    # If we enriched before, get the enrichment
    enrichment = get_enrichment(tenant_id, delete_alert.fingerprint)
    if enrichment:
        deleted_last_received = enrichment.enrichments.get("deletedAt", [])
        assignees_last_receievd = enrichment.enrichments.get("assignees", {})

    if (
        delete_alert.restore is True
        and delete_alert.lastReceived in deleted_last_received
    ):
        # Restore deleted alert
        deleted_last_received.remove(delete_alert.lastReceived)
    elif (
        delete_alert.restore is False
        and delete_alert.lastReceived not in deleted_last_received
    ):
        # Delete the alert if it's not already deleted (wtf basically, shouldn't happen)
        deleted_last_received.append(delete_alert.lastReceived)

    if delete_alert.lastReceived not in assignees_last_receievd:
        # auto-assign the deleting user to the alert
        assignees_last_receievd[delete_alert.lastReceived] = user_email

    # overwrite the enrichment
    enrich_alert_db(
        tenant_id=tenant_id,
        fingerprint=delete_alert.fingerprint,
        enrichments={
            "deletedAt": deleted_last_received,
            "assignees": assignees_last_receievd,
        },
    )

    logger.info(
        "Deleted alert successfully",
        extra={
            "tenant_id": tenant_id,
            "restore": delete_alert.restore,
            "fingerprint": delete_alert.fingerprint,
        },
    )
    return {"status": "ok"}


@router.post(
    "/{fingerprint}/assign/{last_received}", description="Assign alert to user"
)
def assign_alert(
    fingerprint: str,
    last_received: str,
    unassign: bool = False,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
) -> dict[str, str]:
    tenant_id = authenticated_entity.tenant_id
    user_email = authenticated_entity.email
    logger.info(
        "Assigning alert",
        extra={
            "fingerprint": fingerprint,
            "tenant_id": tenant_id,
        },
    )

    assignees_last_receievd = {}  # the last received(s) that are assigned to someone
    enrichment = get_enrichment(tenant_id, fingerprint)
    if enrichment:
        assignees_last_receievd = enrichment.enrichments.get("assignees", {})

    if unassign:
        assignees_last_receievd.pop(last_received, None)
    else:
        assignees_last_receievd[last_received] = user_email

    enrich_alert_db(
        tenant_id=tenant_id,
        fingerprint=fingerprint,
        enrichments={"assignees": assignees_last_receievd},
    )

    try:
        if not unassign:  # if we're assigning the alert to someone, send email
            logger.info("Sending assign alert email to user")
            # TODO: this should be changed to dynamic url but we don't know what's the frontend URL
            keep_platform_url = config(
                "KEEP_PLATFORM_URL", default="https://platform.keephq.dev"
            )
            url = f"{keep_platform_url}/alerts?fingerprint={fingerprint}"
            send_email(
                to_email=user_email,
                template_id=EmailTemplates.ALERT_ASSIGNED_TO_USER,
                url=url,
            )
            logger.info("Sent assign alert email to user")
    except Exception as e:
        logger.exception(
            "Failed to send email to user",
            extra={
                "error": str(e),
                "tenant_id": tenant_id,
                "user_email": user_email,
            },
        )

    logger.info(
        "Assigned alert successfully",
        extra={
            "tenant_id": tenant_id,
            "fingerprint": fingerprint,
        },
    )
    return {"status": "ok"}


# this is super important function and does three things:
# 0. Checks for deduplications using alertdeduplicator
# 1. adds the alerts to the DB
# 2. runs workflows based on the alerts
# 3. runs the rules engine
# 4. update the presets
# TODO: add appropriate logs, trace and all of that so we can track errors
def handle_formatted_events(
    tenant_id,
    provider_type,
    session: Session,
    raw_events: list[dict],
    formatted_events: list[AlertDto],
    pusher_client: Pusher,
    provider_id: str | None = None,
):
    logger.info(
        "Asyncronusly adding new alerts to the DB",
        extra={
            "provider_type": provider_type,
            "num_of_alerts": len(formatted_events),
            "provider_id": provider_id,
            "tenant_id": tenant_id,
        },
    )
    # first, filter out any deduplicated events
    alert_deduplicator = AlertDeduplicator(tenant_id)

    for event in formatted_events:
        event_hash, event_deduplicated = alert_deduplicator.is_deduplicated(event)
        event.alert_hash = event_hash
        event.isDuplicate = event_deduplicated

    # filter out the deduplicated events
    formatted_events = list(
        filter(lambda event: not event.isDuplicate, formatted_events)
    )

    try:
        # keep raw events in the DB if the user wants to
        # this is mainly for debugging and research purposes
        if os.environ.get("KEEP_STORE_RAW_ALERTS", "false") == "true":
            for raw_event in raw_events:
                alert = AlertRaw(
                    tenant_id=tenant_id,
                    raw_alert=raw_event,
                )
                session.add(alert)
        enriched_formatted_events = []
        for formatted_event in formatted_events:
            formatted_event.pushed = True

            enrichments_bl = EnrichmentsBl(tenant_id, session)
            # Post format enrichment
            try:
                formatted_event = enrichments_bl.run_extraction_rules(formatted_event)
            except Exception:
                logger.exception("Failed to run post-formatting extraction rules")

            # Make sure the lastReceived is a valid date string
            # tb: we do this because `AlertDto` object lastReceived is a string and not a datetime object
            # TODO: `AlertDto` object `lastReceived` should be a datetime object so we can easily validate with pydantic
            if not formatted_event.lastReceived:
                formatted_event.lastReceived = datetime.datetime.now(
                    tz=datetime.timezone.utc
                ).isoformat()
            else:
                try:
                    dateutil.parser.isoparse(formatted_event.lastReceived)
                except ValueError:
                    logger.warning("Invalid lastReceived date, setting to now")
                    formatted_event.lastReceived = datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ).isoformat()

            alert = Alert(
                tenant_id=tenant_id,
                provider_type=provider_type,
                event=formatted_event.dict(),
                provider_id=provider_id,
                fingerprint=formatted_event.fingerprint,
                alert_hash=formatted_event.alert_hash,
            )
            session.add(alert)
            session.flush()
            session.refresh(alert)
            formatted_event.event_id = str(alert.id)
            alert_dto = AlertDto(**formatted_event.dict())

            # Mapping
            try:
                enrichments_bl.run_mapping_rules(alert_dto)
            except Exception:
                logger.exception("Failed to run mapping rules")

            alert_enrichment = get_enrichment(
                tenant_id=tenant_id, fingerprint=formatted_event.fingerprint
            )
            if alert_enrichment:
                for enrichment in alert_enrichment.enrichments:
                    # set the enrichment
                    value = alert_enrichment.enrichments[enrichment]
                    setattr(alert_dto, enrichment, value)
            if pusher_client:
                try:
                    pusher_client.trigger(
                        f"private-{tenant_id}",
                        "async-alerts",
                        json.dumps([alert_dto.dict()]),
                    )
                except Exception:
                    logger.exception("Failed to push alert to the client")
            enriched_formatted_events.append(alert_dto)
        session.commit()
        logger.info(
            "Asyncronusly added new alerts to the DB",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
    except Exception:
        logger.exception(
            "Failed to push alerts to the DB",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
    try:
        # Now run any workflow that should run based on this alert
        # TODO: this should publish event
        workflow_manager = WorkflowManager.get_instance()
        # insert the events to the workflow manager process queue
        logger.info("Adding events to the workflow manager queue")
        workflow_manager.insert_events(tenant_id, enriched_formatted_events)
        logger.info("Added events to the workflow manager queue")
    except Exception:
        logger.exception(
            "Failed to run workflows based on alerts",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )

    # Now we need to run the rules engine
    try:
        rules_engine = RulesEngine(tenant_id=tenant_id)
        grouped_alerts = rules_engine.run_rules(formatted_events)
        # if new grouped alerts were created, we need to push them to the client
        if grouped_alerts:
            logger.info("Adding group alerts to the workflow manager queue")
            workflow_manager.insert_events(tenant_id, grouped_alerts)
            logger.info("Added group alerts to the workflow manager queue")
            # Now send the grouped alerts to the client
            logger.info("Sending grouped alerts to the client")
            for grouped_alert in grouped_alerts:
                if pusher_client:
                    try:
                        pusher_client.trigger(
                            f"private-{tenant_id}",
                            "async-alerts",
                            json.dumps([grouped_alert.dict()]),
                        )
                    except Exception:
                        logger.exception("Failed to push alert to the client")
            logger.info("Sent grouped alerts to the client")
    except Exception:
        logger.exception(
            "Failed to run rules engine",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
    # Now we need to update the presets
    try:
        presets = get_all_presets(tenant_id)
        presets_do_update = []
        for preset in presets:
            # filter the alerts based on the search query
            preset_dto = PresetDto(**preset.dict())
            filtered_alerts = RulesEngine.filter_alerts(
                enriched_formatted_events, preset_dto.cel_query
            )
            # if not related alerts, no need to update
            if not filtered_alerts:
                continue
            presets_do_update.append(preset_dto)
            preset_dto.alerts_count = len(filtered_alerts)
            # update noisy
            if preset.is_noisy:
                firing_filtered_alerts = list(
                    filter(
                        lambda alert: alert.status == AlertStatus.FIRING.value,
                        filtered_alerts,
                    )
                )
                # if there are firing alerts, then do noise
                if firing_filtered_alerts:
                    logger.info("Noisy preset is noisy")
                    preset_dto.should_do_noise_now = True
            # else if at least one of the alerts has isNoisy and should fire:
            elif any(
                alert.isNoisy and alert.status == AlertStatus.FIRING.value
                for alert in filtered_alerts
                if hasattr(alert, "isNoisy")
            ):
                logger.info("Noisy preset is noisy")
                preset_dto.should_do_noise_now = True
        # send with pusher
        if pusher_client:
            try:
                pusher_client.trigger(
                    f"private-{tenant_id}",
                    "async-presets",
                    json.dumps([p.dict() for p in presets_do_update], default=str),
                )
            except Exception:
                logger.exception("Failed to send presets via pusher")
    except Exception:
        logger.exception(
            "Failed to send presets via pusher",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )


@router.post(
    "/event",
    description="Receive a generic alert event",
    response_model=AlertDto | list[AlertDto],
    status_code=201,
)
async def receive_generic_event(
    event: AlertDto | list[AlertDto] | dict,
    bg_tasks: BackgroundTasks,
    fingerprint: str | None = None,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
    session: Session = Depends(get_session),
    pusher_client: Pusher = Depends(get_pusher_client),
):
    """
    A generic webhook endpoint that can be used by any provider to send alerts to Keep.

    Args:
        alert (AlertDto | list[AlertDto]): The alert(s) to be sent to Keep.
        bg_tasks (BackgroundTasks): Background tasks handler.
        tenant_id (str, optional): Defaults to Depends(verify_api_key).
        session (Session, optional): Defaults to Depends(get_session).
    """
    tenant_id = authenticated_entity.tenant_id

    enrichments_bl = EnrichmentsBl(tenant_id, session)
    # Pre format enrichment
    try:
        event = enrichments_bl.run_extraction_rules(event)
    except Exception:
        logger.exception("Failed to run pre-formatting extraction rules")

    if isinstance(event, dict):
        event = [AlertDto(**event)]

    if isinstance(event, AlertDto):
        event = [event]

    for _alert in event:
        # if not source, set it to keep
        if not _alert.source:
            _alert.source = ["keep"]

        if fingerprint:
            _alert.fingerprint = fingerprint

        if authenticated_entity.api_key_name:
            _alert.apiKeyRef = authenticated_entity.api_key_name

    bg_tasks.add_task(
        handle_formatted_events,
        tenant_id,
        event[0].source[0],
        session,
        event,
        event,
        pusher_client,
    )

    return event


@router.post(
    "/event/{provider_type}", description="Receive an alert event from a provider"
)
async def receive_event(
    provider_type: str,
    request: Request,
    bg_tasks: BackgroundTasks,
    provider_id: str | None = None,
    fingerprint: str | None = None,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
    session: Session = Depends(get_session),
    pusher_client: Pusher = Depends(get_pusher_client),
) -> dict[str, str]:
    tenant_id = authenticated_entity.tenant_id
    provider_class = ProvidersFactory.get_provider_class(provider_type)
    # if this request is just to confirm the sns subscription, return ok
    # TODO: think of a more elegant way to do this
    # Get the raw body as bytes
    body = await request.body()
    # Parse the raw body
    body = provider_class.parse_event_raw_body(body)
    # Start process the event
    # Attempt to parse as JSON if the content type is not text/plain
    # content_type = request.headers.get("Content-Type")
    # For example, SNS events (https://docs.aws.amazon.com/sns/latest/dg/SendMessageToHttp.prepare.html)
    try:
        event = json.loads(body.decode())
        event_copy = copy.copy(event)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # else, process the event
    logger.info(
        "Handling event",
        extra={
            "provider_type": provider_type,
            "provider_id": provider_id,
            "tenant_id": tenant_id,
        },
    )

    enrichments_bl = EnrichmentsBl(tenant_id, session)
    # Pre format enrichment
    try:
        enrichments_bl.run_extraction_rules(event)
    except Exception as exc:
        logger.warning(
            "Failed to run pre-formatting extraction rules",
            extra={"exception": str(exc)},
        )

    try:
        # Each provider should implement a format_alert method that returns an AlertDto
        # object that will later be returned to the client.
        logger.info(
            f"Trying to format alert with {provider_type}",
            extra={
                "provider_type": provider_type,
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )

        # if we have provider id, let's try to init the provider class with it
        provider_instance = None
        if provider_id:
            try:
                provider_instance = ProvidersFactory.get_installed_provider(
                    tenant_id, provider_id, provider_type
                )
            except Exception as e:
                logger.warning(f"Failed to get provider instance due to {str(e)}")

        formatted_events = provider_class.format_alert(event, provider_instance)

        if isinstance(formatted_events, AlertDto):
            # override the fingerprint if it's provided
            if fingerprint:
                formatted_events.fingerprint = fingerprint
            formatted_events = [formatted_events]

        logger.info(
            f"Formatted alerts with {provider_type}",
            extra={
                "provider_type": provider_type,
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
        # If the format_alert does not return an AlertDto object, it means that the event
        # should not be pushed to the client.
        if formatted_events:
            bg_tasks.add_task(
                handle_formatted_events,
                tenant_id,
                provider_type,
                session,
                event_copy if isinstance(event_copy, list) else [event_copy],
                formatted_events,
                pusher_client,
                provider_id,
            )
        logger.info(
            "Handled event successfully",
            extra={
                "provider_type": provider_type,
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
        return {"status": "ok"}
    except Exception as e:
        logger.exception(
            "Failed to handle event", extra={"error": str(e), "tenant_id": tenant_id}
        )
        raise HTTPException(400, "Failed to handle event")


@router.get(
    "/{fingerprint}",
    description="Get alert by fingerprint",
)
def get_alert(
    fingerprint: str,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
) -> AlertDto:
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        "Fetching alert",
        extra={
            "fingerprint": fingerprint,
            "tenant_id": tenant_id,
        },
    )
    # TODO: once pulled alerts will be in the db too, this should be changed
    all_alerts = get_all_alerts(
        background_tasks=None, authenticated_entity=authenticated_entity, sync=True
    )
    alert = list(filter(lambda alert: alert.fingerprint == fingerprint, all_alerts))
    if alert:
        return alert[0]
    else:
        raise HTTPException(status_code=404, detail="Alert not found")


@router.post(
    "/enrich",
    description="Enrich an alert",
)
def enrich_alert(
    enrich_data: EnrichAlertRequestBody,
    background_tasks: BackgroundTasks,
    pusher_client: Pusher = Depends(get_pusher_client),
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
) -> dict[str, str]:
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        "Enriching alert",
        extra={
            "fingerprint": enrich_data.fingerprint,
            "tenant_id": tenant_id,
        },
    )

    try:
        enrich_alert_db(
            tenant_id=tenant_id,
            fingerprint=enrich_data.fingerprint,
            enrichments=enrich_data.enrichments,
        )
        # get the alert with the new enrichment
        alert = get_alerts_by_fingerprint(
            authenticated_entity.tenant_id, enrich_data.fingerprint, limit=1
        )
        if not alert:
            logger.warning(
                "Alert not found", extra={"fingerprint": enrich_data.fingerprint}
            )
            return {"status": "failed"}

        enriched_alerts_dto = convert_db_alerts_to_dto_alerts(alert)
        # use pusher to push the enriched alert to the client
        if pusher_client:
            logger.info("Pushing enriched alert to the client")
            try:
                pusher_client.trigger(
                    f"private-{tenant_id}",
                    "async-alerts",
                    json.dumps([enriched_alerts_dto[0].dict()]),
                )
                logger.info("Pushed enriched alert to the client")
            except Exception:
                logger.exception("Failed to push alert to the client")
                pass
        logger.info(
            "Alert enriched successfully",
            extra={"fingerprint": enrich_data.fingerprint, "tenant_id": tenant_id},
        )
        return {"status": "ok"}

    except Exception as e:
        logger.exception("Failed to enrich alert", extra={"error": str(e)})
        return {"status": "failed"}


@router.post(
    "/search",
    description="Search alerts",
)
async def search_alerts(
    search_request: SearchAlertsRequest,  # Use the model directly
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
) -> list[AlertDto]:
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        "Searching alerts",
        extra={"tenant_id": tenant_id},
    )
    try:
        search_query = search_request.query
        timeframe_in_seconds = search_request.timeframe
        if timeframe_in_seconds is None:
            timeframe_in_seconds = 86400
        elif timeframe_in_seconds < 0:
            raise HTTPException(
                status_code=400,
                detail="Timeframe cannot be negative",
            )
        # convert the timeframe to days
        timeframe_in_days = timeframe_in_seconds / 86400
        # limit the timeframe to 14 days
        if timeframe_in_days > 14:
            raise HTTPException(
                status_code=400,
                detail="Timeframe cannot be more than 14 days",
            )
        # get the alerts
        alerts = get_alerts_with_filters(
            tenant_id=tenant_id, time_delta=timeframe_in_days
        )
        # convert the alerts to DTO
        alerts_dto = convert_db_alerts_to_dto_alerts(alerts)
        # filter the alerts based on the search query
        filtered_alerts = RulesEngine.filter_alerts(alerts_dto, search_query)
        logger.info(
            "Searched alerts",
            extra={"tenant_id": tenant_id},
        )
        # return the filtered alerts
        return filtered_alerts
    except celpy.celparser.CELParseError as e:
        logger.warning("Failed to parse the search query", extra={"error": str(e)})
        return JSONResponse(
            status_code=400,
            content={
                "error": "Failed to parse the search query",
                "query": search_request.query,
                "line": e.line,
                "column": e.column,
            },
        )

    except Exception as e:
        logger.exception("Failed to search alerts", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to search alerts")
