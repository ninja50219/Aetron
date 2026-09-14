"""Tests for reading Lua and Luau without a Lua interpreter.

The first language Aetron reads that closes blocks with a word rather than a
brace, which is where most of these are aimed: "end" appears inside strings,
inside comments, and inside identifiers, and "elseif ... then" carries an
opening keyword while opening nothing.
"""

from aetron.analyzer.lua_parser import parse
from aetron.analyzer.symbols import SymbolKind


def definitions(result):
    return [s for s in result.symbols if s.kind != SymbolKind.MODULE]


def names(result):
    return [s.name for s in definitions(result)]


def named(result, name):
    return next(s for s in result.symbols if s.name == name)


HARD = '''--!strict
local Players = game:GetService("Players")
local Signal = require(script.Parent.Packages.Signal)

export type Fighter = {
\thealth: number,
}

local CombatService = {}

local MAX_COMBO = 5

function CombatService.new(owner: Player): Fighter
\treturn setmetatable({}, CombatService)
end

function CombatService:takeDamage(amount: number, source: string?): boolean
\tif amount <= 0 then
\t\treturn false
\telseif amount > self.health then
\t\tself.health = 0
\telse
\t\tself.health -= amount
\tend

\tlocal tries = 0
\trepeat
\t\ttries += 1
\tuntil tries >= MAX_COMBO

\tfor i = 1, MAX_COMBO do
\t\tif self.health == 0 then break end
\tend

\tlocal message = [[
\t\ta long string with the word end inside it
\t]]

\t--[[ a block comment
\t     function ghostFunction() end
\t]]

\treturn true
end

local helper = function(a, b, ...)
\treturn a + b
end

return CombatService
'''


class TestDeclarations:
    def test_a_dot_function_is_a_method_of_its_table(self):
        symbol = named(parse(HARD, "a.luau"), "new")
        assert symbol.qualified_name == "CombatService.new"

    def test_a_colon_function_gets_an_implicit_self(self):
        assert named(parse(HARD, "a.luau"), "takeDamage").parameters == [
            "self",
            "amount",
            "source",
        ]

    def test_a_plain_local_function(self):
        result = parse("local function go(a)\n\treturn a\nend\n", "a.lua")
        symbol = named(result, "go")
        assert symbol.kind == SymbolKind.FUNCTION
        assert symbol.parameters == ["a"]

    def test_a_function_expression_bound_to_a_name(self):
        assert named(parse(HARD, "a.luau"), "helper").parameters == ["a", "b", "..."]

    def test_a_module_table(self):
        assert named(parse(HARD, "a.luau"), "CombatService").kind == SymbolKind.CLASS

    def test_a_constant(self):
        assert "MAX_COMBO" in names(parse(HARD, "a.luau"))

    def test_a_luau_type_declaration(self):
        assert "Fighter" in names(parse(HARD, "a.luau"))

    def test_type_annotations_are_stripped_from_parameters(self):
        result = parse("function f(x: number, y: string?)\nend\n", "a.luau")
        assert named(result, "f").parameters == ["x", "y"]


class TestBlockExtent:
    """Counting "end" is not counting braces."""

    def test_a_function_ends_at_its_end(self):
        symbol = named(parse(HARD, "a.luau"), "new")
        assert (symbol.line, symbol.end_line) == (13, 15)

    def test_nested_blocks_do_not_end_it_early(self):
        symbol = named(parse(HARD, "a.luau"), "takeDamage")
        assert symbol.end_line > symbol.line + 20

    def test_elseif_opens_nothing(self):
        source = (
            "function f(x)\n\tif x then\n\t\treturn 1\n\telseif x then\n"
            "\t\treturn 2\n\tend\nend\n\nfunction after()\nend\n"
        )
        assert named(parse(source, "a.lua"), "f").end_line == 7

    def test_repeat_is_closed_by_until(self):
        source = "function f()\n\trepeat\n\t\tx = 1\n\tuntil x\nend\n"
        assert named(parse(source, "a.lua"), "f").end_line == 5

    def test_a_one_line_function(self):
        source = "function f() return 1 end\n\nfunction g() end\n"
        assert named(parse(source, "a.lua"), "f").end_line == 1


class TestNothingIsInvented:
    def test_end_inside_a_long_string_does_not_close_a_block(self):
        assert named(parse(HARD, "a.luau"), "takeDamage").end_line > 40

    def test_a_function_inside_a_block_comment_is_not_a_function(self):
        assert "ghostFunction" not in names(parse(HARD, "a.luau"))

    def test_a_local_inside_a_function_is_not_top_level(self):
        found = names(parse(HARD, "a.luau"))
        assert "tries" not in found
        assert "message" not in found

    def test_end_inside_an_identifier_is_not_a_keyword(self):
        source = "function f()\n\tlocal weekend = 1\n\tlocal appended = 2\nend\n"
        assert named(parse(source, "a.lua"), "f").end_line == 4


class TestRequires:
    def test_a_require_is_recorded_as_written(self):
        assert [i.module for i in parse(HARD, "a.luau").imports] == [
            "script.Parent.Packages.Signal"
        ]

    def test_a_require_in_a_comment_is_not_a_require(self):
        source = '-- require(script.Fake)\nlocal a = require(script.Real)\n'
        assert [i.module for i in parse(source, "a.lua").imports] == ["script.Real"]


class TestRobustness:
    def test_an_empty_file(self):
        result = parse("", "a.lua")
        assert result.parse_error is None
        assert definitions(result) == []

    def test_an_unterminated_long_string_does_not_raise(self):
        assert parse("local s = [[ never closed\n", "a.lua") is not None

    def test_a_file_is_always_a_module_symbol(self):
        module = next(
            s for s in parse("", "src/Combat.luau").symbols
            if s.kind == SymbolKind.MODULE
        )
        assert module.name == "Combat"
