import asyncio
import discord
from discord.ext import voice_recv


class UtteranceSink(voice_recv.AudioSink):
    """
    Buffers per-speaker PCM (48kHz stereo, already Opus-decoded by voice_recv) and
    hands a speaker's buffer off to on_utterance() once Discord reports they stopped
    speaking. write() / on_voice_member_speaking_stop() run on voice_recv's own receive
    thread, not the bot's event loop, so handoff goes through
    asyncio.run_coroutine_threadsafe — the same pattern spotify_player.py uses for its
    vc.play 'after' callbacks.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, on_utterance, min_utterance_ms: int):
        super().__init__()
        self._loop = loop
        self._on_utterance = on_utterance
        self._min_utterance_ms = min_utterance_ms
        self._buffers: dict[int, bytearray] = {}
        self.paused = False  # set True while the bot is thinking/speaking

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        if self.paused or user is None or user.bot:
            return
        self._buffers.setdefault(user.id, bytearray()).extend(data.pcm)

    # The decorator is required: voice_recv's metaclass only collects methods
    # marked by it into __sink_listeners__, and only those get dispatched.
    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.Member):
        if self.paused or member.bot:
            return
        pcm = self._buffers.pop(member.id, None)
        if not pcm:
            # Speaking was detected but no PCM arrived — usually means packets are
            # being dropped upstream (e.g. DAVE frames failing to decrypt).
            print(f"[voice_call] {member.display_name} spoke but no audio was captured", flush=True)
            return
        duration_ms = (len(pcm) / 4) / 48000 * 1000  # 4 bytes/frame: 16-bit stereo @ 48kHz
        if duration_ms < self._min_utterance_ms:
            print(f"[voice_call] {member.display_name}: ignoring {duration_ms:.0f}ms blip", flush=True)
            return
        print(f"[voice_call] {member.display_name} spoke for {duration_ms:.0f}ms", flush=True)
        asyncio.run_coroutine_threadsafe(self._on_utterance(member, bytes(pcm)), self._loop)

    def cleanup(self):
        self._buffers.clear()
