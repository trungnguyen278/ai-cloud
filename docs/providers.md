# Providers: what is free, as of 2026-10-28

The cloud's chains of providers ([ADR-0003](decisions/0003-conversation-pipeline.md)) take free tiers first: one key of the owner's per provider, never a pool of keys. Free tiers change often: two providers below ended theirs in 2026. This list is checked again every three months, and before a provider joins a chain, against its own pages.

- *Official*: from the provider's own page.
- *Reported*: from third parties, to confirm on the provider's page before relying on it.
- **Data:** what the provider may do with what it is sent, on its free tier. Free tiers often pay for themselves with it (ADR-0003, decision 6).

## Realtime speech (speech to speech)

| Provider | Free | Vietnamese | Data | Source |
|---|---|---|---|---|
| Gemini Live API | Free tier: the native-audio Live models | Yes (`vi-VN`), switching language mid-conversation | Used to improve Google's products | Official |
| OpenAI Realtime API | No free tier | — | — | Official, by absence |

## Speech recognition

| Provider | Free | Vietnamese | Notes | Source |
|---|---|---|---|---|
| Gemini API (audio input, Transcribe Live) | Free tier | Yes | Data used to improve Google's products | Official |
| Groq, Whisper large v3 | About 2 hours of audio an hour and 8 a day | Yes (Whisper) | | Reported |
| Azure AI Speech (F0) | 5 hours a month | Yes | | Official |
| Google Cloud Speech-to-Text V1 | 60 minutes a month | Yes | V2 has no free allowance | Reported |
| Speechmatics | About 480 minutes a month (half real-time) | To confirm | | Reported |
| Cloudflare Workers AI, Whisper | Within 10,000 neurons a day, shared with every model | Yes (Whisper) | | Reported |
| Deepgram, AssemblyAI, Gladia | One-time credits ($200, $50, €50) | To confirm | Last resort: they run out | Reported |

## Language models

| Provider | Free | Notes | Source |
|---|---|---|---|
| Gemini API, Flash and Flash-Lite | Free tier; limits per project, in AI Studio | Data used to improve Google's products | Official |
| Groq (e.g. gpt-oss-120b) | Per model: about 30 requests a minute, 1,000 a day, 200,000 tokens a day | Its models change; two left the free tier in 2026 | Reported |
| OpenRouter, `:free` models | 20 requests a minute; 50 a day, or 1,000 a day once $10 of credit was ever bought | The models behind it vary | Official |
| Mistral, Experiment plan | Limits in its console | Requires agreeing that data trains its models | Reported |
| Cloudflare Workers AI | 10,000 neurons a day, shared with every model | | Reported |
| OpenAI | Complimentary daily tokens, for organizations that share their traffic, where eligible | Only shared traffic; no general free tier | Official |

**Left out:**
- GitHub Models: its free tier is for prototyping, and its terms bar production use;
- Cerebras and SambaNova: reported to have ended their free tiers in 2026.

## Speech synthesis

| Provider | Free | Vietnamese | Source |
|---|---|---|---|
| Gemini API, Flash TTS | Free tier | Yes (`vi-VN`, generally available) | Official |
| Google Cloud Text-to-Speech | A month's free characters by voice family: Standard and WaveNet up to 4 million, Neural2 and Chirp 3 HD up to 1 million | Yes; which families have Vietnamese voices, to confirm | Official, as ranges |
| Azure AI Speech (F0) | A month's free neural characters: about 0.5 million, to confirm | Yes: HoaiMy, NamMinh | Reported |
| FPT.AI, Zalo AI, Viettel AI | No free API quota found; FPT.AI offers a free web tool to try its voices | Yes | Reported |

## The chains, in order

1. **Realtime:** Gemini Live, then the cascaded engine.
2. **Recognition:** Gemini, Groq, Azure, Google Cloud V1, Speechmatics, Cloudflare; then the one-time credits.
3. **Language model:** Gemini Flash, Flash-Lite, Groq, OpenRouter's free models, Cloudflare, Mistral; OpenAI's complimentary tokens only if the owner accepts sharing.
4. **Synthesis:** Gemini Flash TTS, Google Cloud, Azure. The robot keeps one voice where it can: a change of provider changes the voice, so the chain prefers the same voice's provider first.
5. **Past every free tier:** the owner's own paid project, under the daily budget.

Language models and Cloudflare speak the OpenAI-compatible API, so most of them share one adapter. LiveKit has plugins for Google, Azure, Groq, Speechmatics, Deepgram, AssemblyAI and Gladia.

## References

Retrieved 2026-10-28:
- Gemini API: [pricing](https://ai.google.dev/gemini-api/docs/pricing), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits); Google Cloud: [Gemini-TTS languages](https://docs.cloud.google.com/text-to-speech/docs/gemini-tts), [Text-to-Speech pricing](https://cloud.google.com/text-to-speech/pricing).
- [OpenRouter pricing](https://openrouter.ai/pricing), and its [rate limits](https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know).
- [Azure Speech pricing](https://azure.microsoft.com/pricing/details/speech/).
- OpenAI: [sharing API inputs and outputs, complimentary tokens](https://help.openai.com/en/articles/10306912).
- Reported free tiers: [Groq and others](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/), [Cloudflare Workers AI](https://dev.to/sungwoo_lee_e0f26be4a29fd/cloudflare-workers-ai-10000-free-neurons-a-day-then-0011-4d93), [GitHub Models](https://toolchew.com/en/review-github-models-2026/), [speech-to-text free tiers](https://convertaudiototext.com/blog/speech-to-text-api-pricing-2026), [Google Cloud STT](https://costbench.com/software/ai-transcription-apis/google-speech-to-text/free-plan/), [FPT.AI's voices](https://docs.fpt.ai/docs/en/speech/documentation/text-to-speech).
- LiveKit Agents' plugins: [`livekit-agents` on PyPI](https://pypi.org/project/livekit-agents/).
