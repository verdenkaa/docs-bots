"""CPU SDK smoke: real tiny reranker inference and Paddle kernels, no weight downloads.

Run inside deploy/native/Dockerfile's image with --network none. This verifies SDK
compatibility, not quality or loading of the full pretrained server models.
"""


def main():
    import inspect
    import math

    import torch
    from transformers import XLMRobertaConfig, XLMRobertaForSequenceClassification
    import paddle
    from paddleocr import PaddleOCR
    from backends import Backend

    torch.set_num_threads(2)
    torch.manual_seed(1)
    config = XLMRobertaConfig(vocab_size=100, hidden_size=16, num_hidden_layers=1,
                              num_attention_heads=2, intermediate_size=32, num_labels=1)
    model = XLMRobertaForSequenceClassification(config).eval()

    class Tokenizer:
        def __call__(self, pairs, **options):
            assert options["max_length"] == 1024
            assert options["truncation"] is True
            ids = torch.tensor([[0, 5, 6 + n, 2] for n in range(len(pairs))])
            return dict(input_ids=ids, attention_mask=torch.ones_like(ids))

    backend = Backend("reranker", {"profile_id": "synthetic"}, {})
    backend.model, backend.tokenizer = model, Tokenizer()
    result = backend.invoke("rerank", {"query": "synthetic", "candidates": ["first", "second"]})
    assert [item["candidate_index"] for item in result["scores"]] == [0, 1]
    assert all(math.isfinite(item["score"]) and 0 <= item["score"] <= 1 for item in result["scores"])
    parameters = inspect.signature(PaddleOCR).parameters
    assert {"text_detection_model_dir", "text_recognition_model_dir", "use_doc_unwarping"} <= set(parameters)
    assert paddle.matmul(paddle.to_tensor([[1., 2.]]), paddle.to_tensor([[3.], [4.]])).item() == 11
    print("CPU SDK smoke passed: real XLM-RoBERTa inference, Paddle CPU kernel and PaddleOCR constructor")


if __name__ == "__main__":
    main()
