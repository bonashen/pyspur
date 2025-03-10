import json
from typing import Dict, List, Optional

from dotenv import load_dotenv
from jinja2 import Template
from pydantic import BaseModel, Field

from ...utils.pydantic_utils import get_nested_field, json_schema_to_model
from ..base import (
    BaseNode,
    BaseNodeConfig,
    BaseNodeInput,
    BaseNodeOutput,
)
from ._utils import LLMModels, ModelInfo, create_messages, generate_text

load_dotenv()

def repair_json(broken_json_str: str) -> str:
    import re
    from re import Match
    from typing import Dict

    # Handle empty or non-string input
    if not broken_json_str or not broken_json_str.strip():
        return "{}"

    repaired = broken_json_str

    # Convert single quotes to double quotes, but not within already double-quoted strings
    # First, temporarily replace valid double-quoted strings
    placeholder = "PLACEHOLDER"
    quoted_strings: Dict[str, str] = {}
    counter = 0

    def replace_quoted(match: Match[str]) -> str:
        nonlocal counter
        key = f"{placeholder}{counter}"
        quoted_strings[key] = match.group(0)
        counter += 1
        return key

    # Temporarily store valid double-quoted strings
    repaired = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', replace_quoted, repaired)

    # Now convert remaining single quotes to double quotes
    repaired = repaired.replace("'", '"')

    # Restore original double-quoted strings
    for key, value in quoted_strings.items():
        repaired = repaired.replace(key, value)

    # Remove trailing commas before closing brackets/braces
    repaired = re.sub(r",\s*([}\]])", r'\1', repaired)

    # Add missing commas between elements
    repaired = re.sub(r"([}\"])\s*([{\[])", r'\1,\2', repaired)

    # Fix unquoted string values
    repaired = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', repaired)

    # Remove any extra whitespace around colons
    repaired = re.sub(r'\s*:\s*', ':', repaired)

    # If the string is wrapped in extra quotes, remove them
    if repaired.startswith('"') and repaired.endswith('"'):
        repaired = repaired[1:-1]

    # Extract the substring from the first { to the last }
    start = repaired.find('{')
    end = repaired.rfind('}')
    if start != -1 and end != -1:
        repaired = repaired[start:end+1]
    else:
        # If no valid JSON object found, return empty object
        return "{}"

    # Final cleanup of whitespace
    repaired = re.sub(r'\s+', ' ', repaired)

    return repaired


class SingleLLMCallNodeConfig(BaseNodeConfig):
    llm_info: ModelInfo = Field(
        ModelInfo(model=LLMModels.GPT_4O, max_tokens=16384, temperature=0.7),
        description="The default LLM model to use",
    )
    system_message: str = Field(
        "You are a helpful assistant.",
        description="The system message for the LLM",
    )
    user_message: str = Field(
        "",
        description="The user message for the LLM, serialized from input_schema",
    )
    few_shot_examples: Optional[List[Dict[str, str]]] = None
    url_variables: Optional[Dict[str, str]] = Field(
        None,
        description="Optional mapping of URL types (image, video, pdf) to input schema variables for Gemini models",
    )


class SingleLLMCallNodeInput(BaseNodeInput):
    """
    We allow any/all extra fields, so that the entire dictionary passed in
    is available in `input.model_dump()`.
    """

    class Config:
        extra = "allow"


class SingleLLMCallNodeOutput(BaseNodeOutput):
    pass


class SingleLLMCallNode(BaseNode):
    """
    Node type for calling an LLM with structured i/o and support for params in system prompt and user_input.
    """

    name = "single_llm_call_node"
    display_name = "Single LLM Call"
    config_model = SingleLLMCallNodeConfig
    input_model = SingleLLMCallNodeInput
    output_model = SingleLLMCallNodeOutput

    def setup(self) -> None:
        super().setup()
        if self.config.output_json_schema:
            self.output_model = json_schema_to_model(
                json.loads(self.config.output_json_schema),
                self.name,
                SingleLLMCallNodeOutput,
            )  # type: ignore

    async def run(self, input: BaseModel) -> BaseModel:
        # Grab the entire dictionary from the input
        raw_input_dict = input.model_dump()

        # Render system_message
        system_message = Template(self.config.system_message).render(raw_input_dict)

        try:
            # If user_message is empty, dump the entire raw dictionary
            if not self.config.user_message.strip():
                user_message = json.dumps(raw_input_dict, indent=2)
            else:
                user_message = Template(self.config.user_message).render(**raw_input_dict)
        except Exception as e:
            print(f"[ERROR] Failed to render user_message {self.name}")
            print(f"[ERROR] user_message: {self.config.user_message} with input: {raw_input_dict}")
            raise e

        messages = create_messages(
            system_message=system_message,
            user_message=user_message,
            few_shot_examples=self.config.few_shot_examples,
        )

        model_name = LLMModels(self.config.llm_info.model).value

        url_vars: Optional[Dict[str, str]] = None
        # Process URL variables if they exist and we're using a Gemini model
        if self.config.url_variables:
            url_vars = {}
            if "file" in self.config.url_variables:
                # Split the input variable reference (e.g. "input_node.video_url")
                # Get the nested field value using the helper function
                file_value = get_nested_field(self.config.url_variables["file"], input)
                # Always use image_url format regardless of file type
                url_vars["image"] = file_value

        try:
            assistant_message_str = await generate_text(
                messages=messages,
                model_name=model_name,
                temperature=self.config.llm_info.temperature,
                max_tokens=self.config.llm_info.max_tokens,
                json_mode=True,
                url_variables=url_vars,
                output_json_schema=self.config.output_json_schema,
            )
        except Exception as e:
            error_str = str(e)

            # Handle all LiteLLM errors
            if "litellm" in error_str.lower():
                error_message = "An error occurred with the LLM service"
                error_type = "unknown"

                # Extract provider from model name
                provider = model_name.split("/")[0] if "/" in model_name else "unknown"

                # Handle specific known error cases
                if "VertexAIError" in error_str and "The model is overloaded" in error_str:
                    error_type = "overloaded"
                    error_message = "The model is currently overloaded. Please try again later."
                elif "rate limit" in error_str.lower():
                    error_type = "rate_limit"
                    error_message = "Rate limit exceeded. Please try again in a few minutes."
                elif "context length" in error_str.lower() or "maximum token" in error_str.lower():
                    error_type = "context_length"
                    error_message = "Input is too long for the model's context window. Please reduce the input length."
                elif (
                    "invalid api key" in error_str.lower() or "authentication" in error_str.lower()
                ):
                    error_type = "auth"
                    error_message = (
                        "Authentication error with the LLM service. Please check your API key."
                    )
                elif "bad gateway" in error_str.lower() or "503" in error_str:
                    error_type = "service_unavailable"
                    error_message = (
                        "The LLM service is temporarily unavailable. Please try again later."
                    )

                raise Exception(
                    json.dumps(
                        {
                            "type": "model_provider_error",
                            "provider": provider,
                            "error_type": error_type,
                            "message": error_message,
                            "original_error": error_str,
                        }
                    )
                )
            raise e

        try:
            assistant_message_dict = json.loads(assistant_message_str)
        except Exception as e:
            try:
                repaired_str = repair_json(assistant_message_str)
                assistant_message_dict = json.loads(repaired_str)
            except Exception as inner_e:
                error_str = str(inner_e)
                error_message = "An error occurred while parsing and repairing the assistant message"
                error_type = "json_parse_error"
                raise Exception(
                    json.dumps({
                        "type": "parsing_error",
                        "error_type": error_type,
                        "message": error_message,
                        "original_error": error_str,
                        "assistant_message_str": assistant_message_str,
                    })
                )

        # Validate and return
        assistant_message = self.output_model.model_validate(assistant_message_dict)
        return assistant_message


if __name__ == "__main__":
    import asyncio

    from pydantic import create_model

    async def test_llm_nodes():
        # Example 1: Simple test case with a basic user message
        simple_llm_node = SingleLLMCallNode(
            name="WeatherBot",
            config=SingleLLMCallNodeConfig(
                llm_info=ModelInfo(model=LLMModels.GPT_4O, temperature=0.4, max_tokens=100),
                system_message="You are a helpful assistant.",
                user_message="Hello, my name is {{ name }}. I want to ask: {{ question }}",
                url_variables=None,
                output_json_schema=json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "answer": {"type": "string"},
                            "name_of_user": {"type": "string"},
                        },
                        "required": ["answer", "name_of_user"],
                    }
                ),
            ),
        )

        simple_input = create_model(
            "SimpleInput",
            name=(str, ...),
            question=(str, ...),
            __base__=BaseNodeInput,
        ).model_validate(
            {
                "name": "Alice",
                "question": "What is the weather like in New York in January?",
            }
        )

        print("[DEBUG] Testing simple_llm_node now...")
        simple_output = await simple_llm_node(simple_input)
        print("[DEBUG] Test Output from single_llm_call:", simple_output)

    asyncio.run(test_llm_nodes())
