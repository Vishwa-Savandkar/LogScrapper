from logscraper.dotnet.stacktrace_parser import DotNetStackTraceParser
from logscraper.fingerprint import fingerprint_error, normalize_error_message


def test_fingerprint_is_stable_for_volatile_values():
    parser = DotNetStackTraceParser()
    first = parser.parse(
        "System.NullReferenceException: Missing order requestId=9b4f7ce4-9b8a-4e4c-a2e5-a7c8aa14d997\n"
        "   at Checkout.Api.OrderService.Get(Guid id) in C:\\a\\OrderService.cs:line 10"
    )
    second = parser.parse(
        "System.NullReferenceException: Missing order requestId=ac9fbfe5-74e9-4b12-9f11-1f388f37a753\n"
        "   at Checkout.Api.OrderService.Get(Guid id) in C:\\b\\OrderService.cs:line 99"
    )

    assert fingerprint_error(
        exception_type=first.exception_type,
        message=first.message,
        stack_trace=first.stack_trace,
        frames=first.frames,
    ) == fingerprint_error(
        exception_type=second.exception_type,
        message=second.message,
        stack_trace=second.stack_trace,
        frames=second.frames,
    )


def test_normalize_error_message_strips_noisy_ids():
    assert normalize_error_message("Failed requestId=abc-123 at 2026-05-03T02:30:00Z") == (
        "failed requestid=<id> at <timestamp>"
    )
