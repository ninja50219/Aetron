"""Tests for reading C# without a C# compiler.

The failure that matters is a claimed definition that is not there. A missed
method costs a search result; an invented one sends a reader to a line that
means something else, and every level of the retrieval protocol trusts these
line numbers. So most of these assert what is *not* reported.
"""

from aetron.analyzer.csharp_parser import parse
from aetron.analyzer.symbols import SymbolKind


def definitions(result):
    return [s for s in result.symbols if s.kind != SymbolKind.MODULE]


def names(result):
    return [s.name for s in definitions(result)]


def named(result, name):
    return next(s for s in result.symbols if s.name == name)


ALLMAN = """using System;

namespace App.Controllers
{
    public class LoginController : Controller, IDisposable
    {
        private readonly IUserStore _users;
        public string LastUser { get; set; }

        public LoginController(IUserStore users)
        {
            _users = users;
        }

        public async Task<IActionResult> LoginHandler(string user, string password)
        {
            var message = "signed in";
            return View();
        }
    }
}
"""

KANDR = """namespace App {
    public class Service {
        public int Count { get; }
        public void Run(int times) {
            var local = 1;
        }
    }
}
"""


class TestDeclarations:
    def test_a_class_is_found_with_its_bases(self):
        symbol = named(parse(ALLMAN, "a.cs"), "LoginController")
        assert symbol.kind == SymbolKind.CLASS
        assert symbol.bases == ["Controller", "IDisposable"]

    def test_methods_are_found(self):
        assert "LoginHandler" in names(parse(ALLMAN, "a.cs"))

    def test_a_method_carries_its_parameters(self):
        assert named(parse(ALLMAN, "a.cs"), "LoginHandler").parameters == [
            "user",
            "password",
        ]

    def test_a_method_is_qualified_by_its_class(self):
        assert named(parse(ALLMAN, "a.cs"), "LoginHandler").qualified_name == (
            "LoginController.LoginHandler"
        )

    def test_a_constructor_is_found(self):
        constructor = [
            s for s in definitions(parse(ALLMAN, "a.cs"))
            if s.qualified_name == "LoginController.LoginController"
        ]
        assert constructor and constructor[0].parameters == ["users"]

    def test_fields_and_properties_are_found(self):
        found = names(parse(ALLMAN, "a.cs"))
        assert "_users" in found
        assert "LastUser" in found

    def test_using_directives_become_imports(self):
        assert [i.module for i in parse(ALLMAN, "a.cs").imports] == ["System"]

    def test_the_span_of_a_method_covers_its_body(self):
        symbol = named(parse(ALLMAN, "a.cs"), "LoginHandler")
        assert symbol.end_line > symbol.line


class TestBraceStyles:
    """C# is commonly written with the opening brace on the next line, which
    puts a class at the same depth it will be closed at."""

    def test_allman_members_are_found(self):
        assert "LoginHandler" in names(parse(ALLMAN, "a.cs"))

    def test_k_and_r_members_are_found(self):
        found = names(parse(KANDR, "a.cs"))
        assert "Run" in found
        assert "Count" in found

    def test_file_scoped_namespace(self):
        source = "namespace App;\n\npublic class Thing\n{\n    public void Go() { }\n}\n"
        assert "Go" in names(parse(source, "a.cs"))


class TestNothingIsInvented:
    def test_a_local_variable_is_not_a_member(self):
        assert "message" not in names(parse(ALLMAN, "a.cs"))
        assert "local" not in names(parse(KANDR, "a.cs"))

    def test_a_method_call_is_not_a_declaration(self):
        assert "View" not in names(parse(ALLMAN, "a.cs"))

    def test_a_control_statement_is_not_a_declaration(self):
        source = (
            "public class A\n{\n    public void Go()\n    {\n"
            "        if (x) { }\n        while (y) { }\n"
            "        foreach (var item in items) { }\n    }\n}\n"
        )
        found = names(parse(source, "a.cs"))
        assert found == ["A", "Go"]

    def test_a_brace_in_a_comment_does_not_open_a_scope(self):
        source = (
            "public class A\n{\n    // a brace } in a comment\n"
            "    public void Go() { }\n}\n"
        )
        assert "Go" in names(parse(source, "a.cs"))

    def test_a_brace_in_a_string_does_not_open_a_scope(self):
        source = (
            'public class A\n{\n    public void Go()\n    {\n'
            '        var s = "a brace } in a string";\n    }\n'
            '    public void After() { }\n}\n'
        )
        assert "After" in names(parse(source, "a.cs"))

    def test_a_block_comment_is_ignored(self):
        source = (
            "public class A\n{\n    /* public void Hidden() { }\n"
            "       still a comment */\n    public void Real() { }\n}\n"
        )
        found = names(parse(source, "a.cs"))
        assert "Real" in found
        assert "Hidden" not in found


class TestRobustness:
    def test_an_empty_file_is_not_an_error(self):
        result = parse("", "a.cs")
        assert result.parse_error is None
        assert definitions(result) == []

    def test_unbalanced_braces_do_not_raise(self):
        assert parse("public class A\n{\n    public void Go()\n", "a.cs") is not None

    def test_an_unterminated_string_does_not_swallow_the_file(self):
        source = 'public class A\n{\n    var s = "unterminated\n    public void Go() { }\n}\n'
        assert "Go" in names(parse(source, "a.cs"))

    def test_a_file_is_always_a_module_symbol(self):
        module = next(
            s for s in parse("", "pkg/Thing.cs").symbols if s.kind == SymbolKind.MODULE
        )
        assert module.name == "Thing"


class TestOtherTypeKinds:
    def test_interface_struct_record_and_enum(self):
        source = (
            "public interface IThing { }\n"
            "public struct Point { }\n"
            "public record Person(string Name);\n"
            "public enum Colour { Red }\n"
        )
        found = names(parse(source, "a.cs"))
        for name in ("IThing", "Point", "Person", "Colour"):
            assert name in found


HARD = """namespace Deep.Nested.Space;

public abstract partial class Repository<TEntity, TKey> where TEntity : class
{
    protected static readonly Dictionary<string, List<int>> Cache = new();
    public abstract Task<TEntity?> FindAsync(TKey id);
    public virtual IEnumerable<TEntity> All() => Cache.Values;

    public int Total
    {
        get { return Cache.Count; }
    }

    private void Helper(string a, int b = 5, params object[] rest)
    {
        for (int i = 0; i < b; i++) { }
    }

    public class Nested
    {
        public void Inner() { }
    }
}
"""


class TestRealWorldSignatures:
    def test_a_generic_constraint_does_not_hide_the_class(self):
        """"where TEntity : class" after the declaration took the whole type
        and every member with it."""
        assert "Repository" in names(parse(HARD, "a.cs"))

    def test_a_constraint_is_not_mistaken_for_a_base_type(self):
        assert named(parse(HARD, "a.cs"), "Repository").bases == []

    def test_a_nullable_type_argument_is_a_type(self):
        assert "FindAsync" in names(parse(HARD, "a.cs"))

    def test_a_method_with_no_body_is_found(self):
        symbol = named(parse(HARD, "a.cs"), "FindAsync")
        assert symbol.kind == SymbolKind.METHOD
        assert symbol.parameters == ["id"]

    def test_an_expression_bodied_method_is_found(self):
        assert "All" in names(parse(HARD, "a.cs"))

    def test_a_property_with_its_block_on_the_next_line_is_found(self):
        symbol = named(parse(HARD, "a.cs"), "Total")
        assert symbol.line == 9 and symbol.end_line == 12

    def test_default_and_params_arguments_keep_their_names(self):
        assert named(parse(HARD, "a.cs"), "Helper").parameters == ["a", "b", "rest"]

    def test_a_nested_class_is_qualified_by_its_parent(self):
        assert named(parse(HARD, "a.cs"), "Inner").qualified_name == (
            "Repository.Nested.Inner"
        )

    def test_a_loop_variable_is_not_a_member(self):
        assert "i" not in names(parse(HARD, "a.cs"))

    def test_a_positional_record_keeps_its_parameters(self):
        symbol = named(parse("public record Person(string Name, int Age);\n", "a.cs"), "Person")
        assert symbol.parameters == ["Name", "Age"]


TRICKY = """[ApiController]
[Route("api/[controller]")]
public class OrdersController : ControllerBase
{
    [HttpGet("{id}")]
    public async Task<ActionResult<Order>> GetById(int id)
    {
        return Ok();
    }

    public async Task<IActionResult> Create(
        [FromBody] OrderRequest request,
        CancellationToken token)
    {
        return Ok();
    }

    public static Money operator +(Money a, Money b) => new Money();

    public int this[int index] => index;

    public void WithLocal()
    {
        int Helper(int x) => x * 2;
        var result = Helper(3);
    }
}
"""


class TestAwkwardDeclarations:
    def test_attributes_are_not_declarations(self):
        found = names(parse(TRICKY, "a.cs"))
        assert "ApiController" not in found
        assert "HttpGet" not in found

    def test_an_attribute_does_not_hide_the_method_below_it(self):
        assert "GetById" in names(parse(TRICKY, "a.cs"))

    def test_a_signature_wrapped_over_lines_is_found(self):
        """Ordinary in any codebase with a line limit."""
        symbol = named(parse(TRICKY, "a.cs"), "Create")
        assert symbol.parameters == ["request", "token"]

    def test_an_operator_overload_is_found(self):
        assert "operator +" in names(parse(TRICKY, "a.cs"))

    def test_an_indexer_is_found(self):
        assert "this[]" in names(parse(TRICKY, "a.cs"))

    def test_a_local_function_is_not_a_member(self):
        """It is a definition, but not one of the class's."""
        assert "Helper" not in names(parse(TRICKY, "a.cs"))

    def test_a_wrapped_signature_spans_to_its_body(self):
        symbol = named(parse(TRICKY, "a.cs"), "Create")
        assert symbol.end_line > symbol.line + 2
