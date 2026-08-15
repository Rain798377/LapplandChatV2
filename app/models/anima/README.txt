Filigree-Anima checkpoint (not committed to git -- it's a ~4GB binary)

Download from Civitai:
https://civitai.com/models/2851584/filigree-anima

Save it here as:
Filigree-Anima-v2.0.safetensors

This matches ANIMA_MODEL_PATH's default in core/config.py. If you save it
under a different name or location, set ANIMA_MODEL_PATH in .env to match --
modal_imagegen/app.py reads that path when deploying.
