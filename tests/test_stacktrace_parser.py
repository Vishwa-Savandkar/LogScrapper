from logscraper.dotnet.stacktrace_parser import DotNetStackTraceParser


def test_parse_dotnet_exception_with_file_and_line():
    text = (
        "System.NullReferenceException: Object reference not set to an instance of an object.\n"
        "   at Checkout.Api.Controllers.OrderController.Get(Int32 id) in C:\\src\\Checkout.Api\\Controllers\\OrderController.cs:line 42\n"
        "   at Microsoft.AspNetCore.Mvc.Infrastructure.ActionMethodExecutor.Execute(ActionContext actionContext)"
    )

    parsed = DotNetStackTraceParser().parse(text)

    assert parsed.exception_type == "System.NullReferenceException"
    assert parsed.message == "Object reference not set to an instance of an object."
    assert len(parsed.frames) == 2
    assert parsed.frames[0].namespace == "Checkout.Api.Controllers"
    assert parsed.frames[0].class_name == "OrderController"
    assert parsed.frames[0].method_name == "Get"
    assert parsed.frames[0].line_number == 42
