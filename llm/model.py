import os
import time

from typing import Any, Literal

from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage

from dotenv import find_dotenv, load_dotenv
from loguru import logger

load_dotenv(find_dotenv())

class ModelOllama:
  def __init__(
      self,
      provider: Literal["deepseek", "qwen", "gemini", "openai", "openai_small"],
    ):

    start_time = time.time()

    if provider == "deepseek":
      model = os.getenv("DEEPSEEK_MODEL_NAME")
    elif provider == "qwen":
      model = os.getenv("QWEN_MODEL_NAME")
    elif provider == "gemini":
      model = os.getenv("GEMINI_MODEL_NAME")
    elif provider == "openai":
      model=os.getenv("OPENAI_120_MODEL_NAME")
    elif provider == "openai_small":
      model=os.getenv("OPENAI_20_MODEL_NAME")
    else:
      raise ValueError(f"Неподдерживаемый провайдер: {provider}")

    logger.info(f"Инициализация ModelOllama ({provider})...")

    self.provider = provider
    self.model = model

    self.llm = ChatOllama(
      model=self.model,
      temperature=0.1,
    )

    logger.success(f"ModelOllama полностью инициализирована за {time.time() - start_time:.2f} сек.")

    def send_message_structured_outputs(self, user: str, response_model: Any) -> str:
      messages = []
      messages.append(
        SystemMessage(
          content="Ты — ассистент, который извлекает информацию из описаний."
        )
      )

      messages.append(HumanMessage(content=user))

      structured_llm = self.llm.with_structured_output(response_model)

      res = structured_llm.invoke(messages)

      return res