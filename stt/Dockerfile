FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 https://github.com/ggml-org/whisper.cpp .
# CPU-only build — no CUDA/GPU flags. GGML_NATIVE=OFF keeps the binary portable
# across whatever CPU actually ends up running it (still gets AVX2 on the N100).
# BUILD_SHARED_LIBS=OFF statically links libwhisper/libggml into whisper-server —
# otherwise the final stage would need every .so copied over by hand.
RUN cmake -B build -DGGML_NATIVE=OFF -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    && cmake --build build -j --config Release --target whisper-server

FROM debian:bookworm-slim
# libgomp1: ggml's CPU backend uses OpenMP for threading — stays a dynamic system
# library even with BUILD_SHARED_LIBS=OFF, which only statically links the project's own libs.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/whisper-server /usr/local/bin/whisper-server

WORKDIR /models
# Quantized base.en (~57MB) — picked over larger/multilingual models to fit
# comfortably in the NAS's 8GB RAM alongside everything else it runs.
RUN curl -fL -o ggml-base.en-q5_1.bin \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q5_1.bin

EXPOSE 8801
CMD ["whisper-server", "-m", "/models/ggml-base.en-q5_1.bin", "--host", "0.0.0.0", "--port", "8801", "-t", "4"]
