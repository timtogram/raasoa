import httpx

from raasoa.config import settings


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        api_key: str = settings.openai_api_key,
        model: str = settings.openai_embedding_model,
        dimensions: int = settings.embedding_dimensions,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"openai/{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "input": texts,
                    "dimensions": self._dimensions,
                },
            )
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]
