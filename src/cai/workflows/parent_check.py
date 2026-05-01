"""Parent-check workflow — minimal placeholder."""
from __future__ import annotations

import asyncio

from pydantic_graph import BaseNode, End, Graph, GraphRunContext


class _PlaceholderState:
    pass


class _PlaceholderNode(BaseNode[_PlaceholderState]):
    async def run(self, ctx: GraphRunContext[_PlaceholderState]) -> End[None]:
        return End(None)


parent_check_graph = Graph(nodes=[_PlaceholderNode])


def main() -> None:
    asyncio.run(parent_check_graph.run(_PlaceholderNode(), state=_PlaceholderState()))
