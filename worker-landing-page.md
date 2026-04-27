# Worker: Landing Page Rewrite for Compadre (STT)

## Summary
Rewrote `deploy/landing/index.html` from a TTS (Comadre) landing page to an STT (Compadre) landing page.

## Changes Made

### Identity
- **Name**: Comadre → Compadre
- **Tagline**: "Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita" (STT-focused)
- **Meta description**: Updated to speech-to-text context

### Visual Theme
- Background: `#0e0e1a` → `#0d1117`
- Accent color: `#b85515` (orange) → `#58a6ff` (blue)
- All CSS custom properties updated accordingly
- Same Inter + Bungee font stack, same layout patterns, same card/hero/footer structure

### Demo Form
- Replaced text input with **audio file upload** with drag-and-drop
- Accepts: `.opus`, `.ogg`, `.mp3`, `.wav`, `.m4a`
- Fields: API Key, Audio file, Response format (dropdown), Language (default "pt")
- Submit button: "Transcrever" (was "Gerar áudio")

### Result Panel
- Displays **transcription text** (not audio player)
- **Copy-to-clipboard** button with visual feedback ("Copiado!")
- Metadata for verbose_json responses: detected language, duration, segment count

### New Sections
- **API Docs**: POST `/v1/audio/transcriptions` + GET `/health` with descriptions
- **Pricing**: Three-tier (Gratuito, Padrão R$0,04/min, Self-hosted)

### Info Badges
- 30 req/min per key (was per IP)
- 25 MB max file size
- No audio storage
- faster-whisper large-v3 badge

### Footer
- "Powered by Compadre" (was no "Powered by" text)

### Logo
- Kept referencing `/logo-comadre.webp` (rename deferred per task instructions)

### JavaScript
- Removed voice-loading logic (not relevant for STT)
- Added drag-and-drop file handling
- Form submission sends `multipart/form-data` to `/v1/audio/transcriptions`
- Response parsing handles json, verbose_json, text, srt, vtt formats
- Copy-to-clipboard with `navigator.clipboard` API + fallback for older browsers

## File
- `/home/galvani/dev/compadre/deploy/landing/index.html` (1129 lines)
