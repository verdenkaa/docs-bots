"""Pinned native CPU models. SDK imports and weight loading happen only in load()."""

import base64
import io


def windows(ids, size=254, stride=128):
    if not ids:
        return [[]]
    chunks, start = [], 0
    while True:
        chunks.append(ids[start:start + size])
        if start + size >= len(ids):
            return chunks
        start += stride


class Backend:
    def __init__(self, role, binding, description):
        self.role, self.binding, self.description = role, binding, description
        self.model = self.tokenizer = None

    def load(self):
        from huggingface_hub import snapshot_download
        cache = "/var/lib/native/hf"

        def snapshot(repo, revision, patterns):
            return snapshot_download(repo_id=repo, revision=revision, cache_dir=cache,
                                     allow_patterns=patterns)

        if self.role == "ocr":
            from paddleocr import PaddleOCR
            components = self.description["components"]
            paths = {name: snapshot(item["model_id"], item["model_revision"],
                                     ["inference.*", "config.json"])
                     for name, item in components.items()}
            self.model = PaddleOCR(
                text_detection_model_name="PP-OCRv5_server_det", text_detection_model_dir=paths["detector"],
                text_recognition_model_name="eslav_PP-OCRv5_mobile_rec", text_recognition_model_dir=paths["recognizer"],
                use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
                device="cpu", enable_mkldnn=False, cpu_threads=4)
            return
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        torch.set_num_threads(4)
        path = snapshot(self.binding["model_id"], self.binding["model_revision"],
                        ["*.json", "*.safetensors", "*.model"])
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            path, local_files_only=True, trust_remote_code=False, use_safetensors=True).to("cpu").eval()
        if self.role == "injection":
            labels = self.model.config.id2label
            if labels.get(1) != "prompt_injection" or self.model.config.num_labels != 2:
                raise ValueError("Unexpected injection label mapping")
        elif self.model.config.num_labels != 1:
            raise ValueError("Reranker must have one relevance logit")

    def invoke(self, operation, payload):
        profile = self.binding["profile_id"]
        if self.role == "ocr" and operation == "recognize_text":
            import numpy as np
            from PIL import Image
            raw = base64.b64decode(payload["image"]["data"], validate=True)
            if len(raw) > 20 * 1024 * 1024:
                raise ValueError("Image too large")
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError("Image resolution too large")
                bgr = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
            texts = []
            for result in self.model.predict(bgr):
                texts.extend(result.get("rec_texts", []))
            return {"profile_id": profile, "text": "\n".join(texts)}
        import torch
        with torch.inference_mode():
            if self.role == "reranker" and operation == "rerank":
                candidates = payload["candidates"]
                scores = []
                for start in range(0, len(candidates), 4):
                    pairs = [[payload["query"], text] for text in candidates[start:start + 4]]
                    encoded = self.tokenizer(pairs, padding=True, truncation=True, max_length=1024, return_tensors="pt")
                    logits = self.model(**encoded).logits.reshape(-1)
                    scores.extend(torch.sigmoid(logits).tolist())
                return {"profile_id": profile, "scores": [dict(candidate_index=n, score=float(score))
                                                           for n, score in enumerate(scores)]}
            if self.role == "injection" and operation == "classify_injection":
                ids = self.tokenizer(payload["text"], add_special_tokens=False)["input_ids"]
                score = 0.0
                chunks = windows(ids)
                for start in range(0, len(chunks), 4):
                    features = [self.tokenizer.prepare_for_model(chunk, add_special_tokens=True,
                                                                return_attention_mask=True)
                                for chunk in chunks[start:start + 4]]
                    encoded = self.tokenizer.pad(features, padding=True, return_tensors="pt")
                    values = torch.softmax(self.model(**encoded).logits, dim=-1)[:, 1]
                    score = max(score, float(values.max().item()))
                # The business threshold belongs to Guardrails, not the model service.
                return {"profile_id": profile, "score": score, "label": None}
        raise ValueError("Operation does not match the selected role")
