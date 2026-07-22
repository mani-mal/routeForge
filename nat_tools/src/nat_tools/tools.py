import logging
import operator

from pydantic import Field

from nat.plugin_api import Builder
from nat.plugin_api import FunctionBaseConfig
from nat.plugin_api import FunctionInfo
from nat.plugin_api import LLMFrameworkEnum
from nat.plugin_api import register_function

logger = logging.getLogger(__name__)

_OPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
}


class CalculatorFunctionConfig(FunctionBaseConfig, name="calculator"):
    """
    Calculator tool - evaluates a simple two-operand arithmetic expression.
    Does NOT use eval(); only "<number> <op> <number>" with op in + - * / is
    parsed and computed.
    """
    prefix: str = Field(default="Result:", description="Prefix to add before the computed result.")


@register_function(config_type=CalculatorFunctionConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def calculator_function(config: CalculatorFunctionConfig, builder: Builder):
    """
    Registers a calculator function (addressable via `calculator` in the configuration).

    Args:
        config (CalculatorFunctionConfig): The configuration for the function.
        builder (Builder): The builder object.

    Returns:
        FunctionInfo: The function info object for the function.
    """

    async def _calculator(expression: str) -> str:
        """
        Evaluates a simple arithmetic expression of the form "<number> <op> <number>",
        where <op> is one of +, -, *, /. Example: "12 * 7". Safe: parses exactly two
        numbers and one operator, never calls eval() on arbitrary input.

        Args:
            expression (str): The arithmetic expression to evaluate, e.g. "12 * 7".

        Returns:
            str: The computed result, prefixed with the configured prefix.
        """
        logger.info("calculator tool invoked with expression=%r", expression)
        parts = expression.strip().split()
        if len(parts) != 3 or parts[1] not in _OPS:
            return f"{config.prefix} ERROR could not parse expression {expression!r}"
        left, op, right = parts
        try:
            result = _OPS[op](float(left), float(right))
        except Exception as exc:  # noqa: BLE001
            return f"{config.prefix} ERROR {exc}"
        return f"{config.prefix} {result}"

    yield FunctionInfo.from_fn(_calculator, description=_calculator.__doc__)


class DocSearchFunctionConfig(FunctionBaseConfig, name="doc_search"):
    """
    Doc-search tool - FAKE. Returns a canned snippet referencing the query;
    stands in for a future real document-retrieval integration.
    """


@register_function(config_type=DocSearchFunctionConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def doc_search_function(config: DocSearchFunctionConfig, builder: Builder):
    """
    Registers a fake document-search function (addressable via `doc_search` in
    the configuration).

    Args:
        config (DocSearchFunctionConfig): The configuration for the function.
        builder (Builder): The builder object.

    Returns:
        FunctionInfo: The function info object for the function.
    """

    async def _doc_search(query: str) -> str:
        """
        Searches internal documentation for the given query. FAKE: always
        returns a canned snippet referencing the query, no real retrieval.

        Args:
            query (str): The search query.

        Returns:
            str: A canned snippet referencing the query.
        """
        logger.info("doc_search tool invoked with query=%r", query)
        return f"[doc] Top match for '{query}': ...canned excerpt from the internal knowledge base..."

    yield FunctionInfo.from_fn(_doc_search, description=_doc_search.__doc__)


class CrmLookupFunctionConfig(FunctionBaseConfig, name="crm_lookup"):
    """
    CRM-lookup tool - FAKE. Returns a canned account record; stands in for a
    future real CRM integration.
    """


@register_function(config_type=CrmLookupFunctionConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def crm_lookup_function(config: CrmLookupFunctionConfig, builder: Builder):
    """
    Registers a fake CRM-lookup function (addressable via `crm_lookup` in the
    configuration).

    Args:
        config (CrmLookupFunctionConfig): The configuration for the function.
        builder (Builder): The builder object.

    Returns:
        FunctionInfo: The function info object for the function.
    """

    async def _crm_lookup(account: str) -> str:
        """
        Looks up a CRM account record by account name/id. FAKE: always returns
        a canned record, no real CRM call.

        Args:
            account (str): The account name or id to look up.

        Returns:
            str: A canned account record.
        """
        logger.info("crm_lookup tool invoked with account=%r", account)
        return f"[crm] Account {account}: status=active, tier=gold"

    yield FunctionInfo.from_fn(_crm_lookup, description=_crm_lookup.__doc__)
