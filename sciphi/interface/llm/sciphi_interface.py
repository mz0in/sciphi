"""A module for interfacing with local vLLM models"""
import logging
import re
from typing import List, Optional

from copy import copy

from sciphi.interface.base import LLMInterface, LLMProviderName, RAGInterface
from sciphi.interface.llm_interface_manager import llm_interface
from sciphi.llm import GenerationConfig, SciPhiConfig, SciPhiLLM

logger = logging.getLogger(__name__)


class SciPhiFormatter:
    """Formatter for SciPhi."""

    SYSTEM_PROMPT = "### System:\nYou are a helpful and informative professor. You give long, accurate, and detailed explanations to student questions. You answer EVERY question that is given to you. You retrieve data multiple times if necessary.\n\n"
    INSTRUCTION_PREFIX = "### Instruction:\n"
    INSTRUCTION_SUFFIX = "### Response:\n"
    INIT_PARAGRAPH_TOKEN = "<paragraph>"
    END_PARAGRAPH_TOKEN = "</paragraph>"

    RETRIEVAL_TOKEN = "[Retrieval]"
    IRRELEVANT_TOKEN = "[Irrelevant]"
    FULLY_SUPPORTED = "[Fully supported]"
    NO_RETRIEVAL_TOKEN = "[No Retrieval]"
    EVIDENCE_TOKEN = "[Continue to Use Evidence]"
    UTILITY_TOKEN = "[Utility:5]"
    RELEVANT_TOKEN = "[Relevant]"
    PARTIALLY_SUPPORTED_TOKEN = "[Partially supported]"
    NO_SUPPORT_TOKEN = "[No support / Contradictory]"
    END_TOKEN = "</s>"

    @staticmethod
    def format_prompt(input: str) -> str:
        """Format the prompt for the model."""
        return f"{SciPhiFormatter.SYSTEM_PROMPT}{SciPhiFormatter.INSTRUCTION_PREFIX}\n{input}\n\n{SciPhiFormatter.INSTRUCTION_SUFFIX}"

    @staticmethod
    def extract_post_prompt(completion: str) -> str:
        if SciPhiFormatter.INSTRUCTION_SUFFIX not in completion:
            raise ValueError(
                f"Full Completion does not contain {SciPhiFormatter.INSTRUCTION_SUFFIX}"
            )

        return completion.split(SciPhiFormatter.INSTRUCTION_SUFFIX)[1]

    @staticmethod
    def remove_cruft(result: str) -> str:
        pattern = f"{re.escape(SciPhiFormatter.INIT_PARAGRAPH_TOKEN)}.*?{re.escape(SciPhiFormatter.END_PARAGRAPH_TOKEN)}"
        # Remove <paragraph>{arbitrary text...}</paragraph>
        result = re.sub(pattern, "", result, flags=re.DOTALL)

        return (
            result.replace(SciPhiFormatter.RETRIEVAL_TOKEN, "")
            .replace(SciPhiFormatter.NO_RETRIEVAL_TOKEN, "")
            .replace(SciPhiFormatter.IRRELEVANT_TOKEN, "")
            .replace(SciPhiFormatter.EVIDENCE_TOKEN, "")
            .replace(SciPhiFormatter.UTILITY_TOKEN, "")
            .replace(SciPhiFormatter.RELEVANT_TOKEN, "")
            .replace(SciPhiFormatter.PARTIALLY_SUPPORTED_TOKEN, "")
            .replace(SciPhiFormatter.FULLY_SUPPORTED, "")
            .replace(SciPhiFormatter.END_TOKEN, "")
            .replace(SciPhiFormatter.NO_SUPPORT_TOKEN, "")
        )


@llm_interface
class SciPhiLLMInterface(LLMInterface):
    """A class to interface with local vLLM models."""

    provider_name = LLMProviderName.SCIPHI
    ALPACA_CHAT_SYSTEM_PROMPT = "Below is a series of instructions from a USER that describes a task, paired with an input that provides further context. The ASSISTANT writes a response that concisely and appropriately completes the request."

    def __init__(
        self,
        config: SciPhiConfig = SciPhiConfig(),
        rag_interface: Optional[RAGInterface] = None,
        *args,
        **kwargs,
    ) -> None:
        self._model = SciPhiLLM(config)
        self.rag_interface = rag_interface

    def get_chat_completion(
        self, conversation: list[dict], generation_config: GenerationConfig
    ) -> str:
        self._check_stop_token(generation_config.stop_token)
        prompt = ""
        added_system_prompt = False
        for message in conversation:
            if message["role"] == "system":
                prompt += f"### System:\n{SciPhiLLMInterface.ALPACA_CHAT_SYSTEM_PROMPT}. Further, the assistant is given the following additional instructions - {message['content']}\n\n"
                added_system_prompt = True
            elif message["role"] == "user":
                last_user_message = message["content"]
                prompt += f"### Instruction:\n{last_user_message}\n\n"
            elif message["role"] == "assistant":
                prompt += f"### Response:\n{message['content']}\n\n"

        if not added_system_prompt:
            prompt = f"### System:\n{SciPhiLLMInterface.ALPACA_CHAT_SYSTEM_PROMPT}.\n\n{prompt}"

        # TODO - Cleanup RAG logic checks across this script.

        if not generation_config.model_name:
            raise ValueError("No model name provided")
        if "RAG" in generation_config.model_name:
            if not self.rag_interface:
                raise ValueError(
                    "RAG generation requested but no RAG interface provided"
                )
            generation_config_copy = copy(generation_config)
            generation_config_copy.stop_token = None
            if len(conversation) > 1:
                context_query = SciPhiFormatter.remove_cruft(
                    self.model.get_instruct_completion(
                        f"### Instruction:\nBased on the following conversation, what is the ideal query to retrieve related context? ### Conversation:\n{prompt}\n\nNow, return the query.\n\n### Response:\n",
                        generation_config_copy,
                    )
                )
            else:
                context_query = last_user_message
            print("context_query = ", context_query)
            context = self.rag_interface.get_contexts([context_query])[0]
            prompt += f"### Response:\n{SciPhiFormatter.RETRIEVAL_TOKEN} {SciPhiFormatter.INIT_PARAGRAPH_TOKEN}{context}{SciPhiFormatter.END_PARAGRAPH_TOKEN}"
        else:
            prompt += f"### Response:\n"
        latest_completion = self.model.get_instruct_completion(
            prompt, generation_config
        )

        return SciPhiFormatter.remove_cruft(latest_completion)

    def get_completion(
        self, prompt: str, generation_config: GenerationConfig
    ) -> str:
        """Get a completion from the local vLLM provider."""
        self._check_stop_token(generation_config.stop_token)
        logger.debug(
            f"Requesting completion from local vLLM with model={generation_config.model_name} and prompt={prompt}"
        )

        # TODO - Cleanup and consolidate RAG logic checks across this script.

        if not generation_config.model_name:
            raise ValueError("No model name provided")

        if "RAG" not in generation_config.model_name:
            return self.model.get_instruct_completion(
                prompt, generation_config
            ).strip()
        if not self.rag_interface:
            raise ValueError(
                "RAG model requested, but no RAG interface provided"
            )
        completion = ""
        while True:
            prompt_with_context = (
                SciPhiFormatter.format_prompt(prompt) + completion
            )
            latest_completion = self.model.get_instruct_completion(
                prompt_with_context, generation_config
            ).strip()
            completion += latest_completion

            if not completion.endswith(SciPhiFormatter.RETRIEVAL_TOKEN):
                break
            context_query = (
                prompt
                if completion == SciPhiFormatter.RETRIEVAL_TOKEN
                else f"{SciPhiFormatter.remove_cruft(completion)}"
            )
            context = self.rag_interface.get_contexts([context_query])[0]
            completion += f"{SciPhiFormatter.INIT_PARAGRAPH_TOKEN}{context}{SciPhiFormatter.END_PARAGRAPH_TOKEN}"
        return SciPhiFormatter.remove_cruft(completion).strip()

    def get_batch_completion(
        self, prompts: List[str], generation_config: GenerationConfig
    ) -> List[str]:
        """Get a completion from the local vLLM provider."""

        logger.debug(
            f"Requesting completion from local vLLM with model={generation_config.model_name} and prompts={prompts}"
        )
        return self.model.get_batch_instruct_completion(
            prompts, generation_config
        )

    def _check_stop_token(self, stop_token: Optional[str]) -> None:
        if stop_token != SciPhiFormatter.INIT_PARAGRAPH_TOKEN:
            raise ValueError(
                f"Must speicfy stop_token = {SciPhiFormatter.INIT_PARAGRAPH_TOKEN} to run with SciPhi"
            )

    @property
    def model(self) -> SciPhiLLM:
        return self._model
