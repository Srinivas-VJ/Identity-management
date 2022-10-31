from typing import Coroutine, Union

from ....connections.models.conn_record import ConnRecord
from ....core.error import BaseError

from .messages.credential_problem_report import (
    CredentialProblemReport,
    ProblemReportReason,
)
from .models.credential_exchange import V10CredentialExchange


def problem_report_for_record(
    record: Union[ConnRecord, V10CredentialExchange],
    desc_en: str,
) -> CredentialProblemReport:
    """
    Create problem report for record.

    Args:
        record: connection or exchange record
        desc_en: description text to include in problem report

    """
    result = CredentialProblemReport(
        description={
            "en": desc_en,
            "code": ProblemReportReason.ISSUANCE_ABANDONED.value,
        },
    )
    if record:
        thid = getattr(record, "thread_id", None)
        if thid:
            result.assign_thread_id(thid)

    return result


async def report_problem(
    err: BaseError,
    desc_en: str,
    http_error_class,
    record: Union[ConnRecord, V10CredentialExchange],
    outbound_handler: Coroutine,
):
    """
    Send problem report response and raise corresponding HTTP error.

    Args:
        err: error for internal diagnostics
        desc_en: description text to include in problem report (response)
        http_error_class: HTTP error to raise
        record: record to cite by thread in problem report
        outbound_handler: outbound message handler

    """
    if record:
        await outbound_handler(
            problem_report_for_record(record, desc_en),
            connection_id=record.connection_id,
        )

    raise http_error_class(reason=err.roll_up) from err
