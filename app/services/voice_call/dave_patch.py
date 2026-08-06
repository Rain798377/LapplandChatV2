"""
Inbound DAVE (E2EE) decryption for discord-ext-voice-recv — vendored patch.

Discord began enforcing the DAVE end-to-end encryption protocol on voice channels
in March 2026. discord.py 2.7+ negotiates it (that's the `davey` dependency), so
received frames are *still* MLS-encrypted after transport decryption — feeding
them straight to opus fails with `OpusError: corrupted stream` on every packet.

discord-ext-voice-recv has no DAVE support of its own, and the upstream PRs adding
it are unmerged, so the decrypt step is patched in here instead. It reuses the
DaveSession discord.py already maintains for the same connection.

Mirrors upstream PR #54 (imayhaveborkedit/discord-ext-voice-recv#54), the fix
reported working on discord.py 2.7.1. Delete this module and its apply() call in
pipeline.py once upstream ships DAVE support.
"""

import logging

from discord.ext.voice_recv.opus import PacketDecoder, VoiceData

try:
    from davey import MediaType
    _has_dave = True
except ImportError:
    _has_dave = False

log = logging.getLogger(__name__)

_applied = False
_orig_init = PacketDecoder.__init__
_orig_decode_packet = PacketDecoder._decode_packet


def _get_dave_session(decoder: PacketDecoder):
    connection = getattr(decoder.sink.voice_client, "_connection", None)
    return getattr(connection, "dave_session", None)


def _patched_init(self, router, ssrc: int):
    _orig_init(self, router, ssrc)
    # Tolerate unencrypted frames (senders mid-transition, or not E2EE-capable)
    # instead of rejecting them outright.
    session = _get_dave_session(self)
    if session is not None:
        try:
            session.set_passthrough_mode(True, 10)
        except Exception as e:
            log.debug("could not enable DAVE passthrough mode: %s", e)


def _patched_process_packet(self, packet) -> VoiceData:
    # Resolve the speaker first — DAVE keys ratchet per sender, so decryption
    # needs their user id. (Upstream resolves this *after* decoding.)
    member = self._get_cached_member()
    if member is None:
        self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
        member = self._get_cached_member()

    if (
        _has_dave
        and member is not None
        and packet.decrypted_data
        and not packet.is_silence()
    ):
        session = _get_dave_session(self)
        if session is not None and session.ready:
            try:
                packet.decrypted_data = session.decrypt(
                    member.id, MediaType.audio, bytes(packet.decrypted_data)
                )
            except Exception as e:
                # Undecryptable frame — return it with no source so it gets
                # dropped rather than decoded into garbage.
                log.debug("DAVE decrypt failed for ssrc %s: %s", self.ssrc, e)
                self._last_seq = packet.sequence
                self._last_ts = packet.timestamp
                return VoiceData(packet, None, pcm=b"")

    pcm = None
    if not self.sink.wants_opus():
        packet, pcm = self._decode_packet(packet)

    data = VoiceData(packet, member, pcm=pcm)
    self._last_seq = packet.sequence
    self._last_ts = packet.timestamp
    return data


def _patched_decode_packet(self, packet):
    if packet:
        assert self._decoder is not None
        try:
            pcm = self._decoder.decode(packet.decrypted_data, fec=False)
        except Exception:
            # Fall back to opus packet-loss concealment. An exception escaping
            # here kills the router thread, which tears down the whole session.
            pcm = self._decoder.decode(None, fec=False)
        return packet, pcm
    return _orig_decode_packet(self, packet)


def apply() -> None:
    """Patch DAVE decryption into voice_recv's packet decoder. Idempotent."""
    global _applied
    if _applied:
        return
    if not _has_dave:
        log.warning("davey not installed — inbound voice will not decrypt if DAVE is active")
        return

    PacketDecoder.__init__ = _patched_init
    PacketDecoder._process_packet = _patched_process_packet
    PacketDecoder._decode_packet = _patched_decode_packet
    _applied = True
    log.info("DAVE inbound decryption patch applied to voice_recv")
