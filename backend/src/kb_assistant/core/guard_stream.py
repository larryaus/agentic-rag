from .ports import InputVerdict, StreamFilter


class PassThroughFilter:
    def feed(self, text: str) -> str:
        return text

    def finish(self, success: bool = True) -> str:
        return ""


class PassThroughGuardrail:
    """Bedrock Converse checks the marked question and masks output in sync mode."""

    async def check_input(self, text: str) -> InputVerdict:
        return InputVerdict(True)

    def output_filter(self) -> StreamFilter:
        return PassThroughFilter()
