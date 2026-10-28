# 0003 — The conversation pipeline: Gemini Live first, a chain of free tiers behind it

- **Status:** Proposed
- **Date:** 2026-10-28
- **Revised:** 2026-10-28: the free tier's limits, and no rotating keys or projects to pass them.

## Context

- Phase one serves one owner, in Vietnamese mixed with English ([Phases](../architecture.md#phases); ai-platform ADR-0002). The owner cannot run models locally, and has little money for APIs ([Budget](../architecture.md#budget-ai-on-little-money)).
- The cloud decides when the user has finished speaking (ai-robot ADR-0012). LiveKit's turn detector has no Vietnamese ([ADR-0002](0002-stack.md)).
- A conversation calls the device's tools and the cloud's skills, all MCP ([ADR-0001](0001-architecture.md)).
- A minute of conversation costs the Live API about 1,700 tokens heard and 1,500 spoken: the price per minute divided by the price per token. An hour of conversation a day is about 100,000 tokens heard, far below a limit per minute. A limit per day, which Google does not publish, is the one to measure.
- What the providers offer, from their own pages:

| Offer | Facts |
|---|---|
| Gemini API, free tier | Free of charge: the Live API's native-audio models (Gemini 3.8 Live, 3.1 Flash Live, 2.5 Flash Native Audio), Flash and Flash-Lite, and the Flash TTS models. Paid, the Live models cost $0.005 a minute heard and $0.018 a minute spoken. Limits are per project, shown in AI Studio. |
| Gemini Live API | Vietnamese (`vi-VN`) among its 24 languages. Native audio switches language within a conversation. An audio session lasts 15 minutes and a connection about 10; context window compression lifts the first limit, session resumption the second. |
| LiveKit Agents | Gemini Live as a realtime model (`livekit.plugins.google`, with our own key). Its turn detection, from voice activity, is on by default. Tool calls are synchronous: the model waits for each result. |
| Gemini API's limits | Applied per project, not per API key, and shown for each project in AI Studio. For the Live API's free tier, third parties report 3 concurrent sessions and 1,000,000 tokens a minute; Google publishes no daily figures. |
| Google APIs' terms | "You agree to, and will not attempt to circumvent, such limitations documented with each API" (§2(d)). Beyond them, Google's express consent is needed. Credentials are "intended to be used by you and identify your API Client", kept confidential, and never embedded in open source projects (§4(b)(1)). |
| Gemini API's terms | On unpaid quota, Google may use prompts and responses to improve its products, and people may review them. Paid use is excluded, and so are users in the EEA, Switzerland and the UK. |
| Groq, free tier | Whisper large v3 recognizes Vietnamese, within about 8 hours of audio a day (third-party figures, to confirm on Groq's page). |
| Azure AI Speech, free tier (F0) | 5 hours of speech recognition a month, and a monthly allowance of neural speech, with Vietnamese voices (HoaiMy, NamMinh). |

## Decision

1. **The first engine is realtime: Gemini Live**, a native-audio model, through LiveKit's Google plugin, on the free tier.
   - The model is the newest Live model the free tier offers, chosen in configuration.
   - Vietnamese and English mix in one conversation, and the system instructions bound it to the home's languages.
   - **The end of speech:** the model's own voice-activity detection decides it. The agent then sends `turn.speech_end` to the device, as the cloud link says.
   - **Tools:** the device's capabilities and the cloud's skills are declared as the model's functions.
   - **Sessions:** context window compression is on, and the agent resumes a session across connections, so a conversation is not cut at 10 or 15 minutes.
2. **The second engine is cascaded**, used when the realtime engine fails, runs out of quota, or lacks a language. Each stage is a chain of official free tiers:
   - recognition: Gemini, then Groq's Whisper large v3, then Azure;
   - the language model: Gemini Flash, then Flash-Lite, then a model on Groq's free tier;
   - speech: Gemini's Flash TTS, then Azure's Vietnamese neural voices;
   - the end of speech: Silero VAD after a silence tuned for Vietnamese, under the device's own fallback (ai-robot ADR-0012).
3. **Metering:** every call records its provider and its units (seconds, tokens, characters).
   - Phase one has one daily budget for the cloud.
   - When a free quota runs out, the chain moves on. When every chain is out, the device shows its offline face rather than nothing.
4. **Memory:** a realtime session remembers nothing between conversations. The agent opens each session with what `memory` holds for the home and the person (summaries, facts), and `worker` writes what is worth keeping after it.
5. **No rotating keys or projects to pass a free tier.**
   - The limits are per project, so more keys gain nothing.
   - Spreading one owner's use over several projects or accounts, to get past them, breaches Google's API terms (§2(d)), at the risk of losing them all.
   - A person's own key, used for that person's own conversations, is not rotation: each key's limit serves its own owner, as the architecture's growth table foresees for a home's own keys. A pool of other people's keys, taken in turn when one runs out, is rotation, even with their owners' consent: the terms bind each owner to keep the key to their own use. It passes one person's limit with another's quota, and Google's terms keep a key to its owner's use (§4(b)(1)).
   - A second project of the owner's with a different purpose is not rotation. One may be paid, under a budget, to take what the free one cannot; one may be for development, to keep tests off the robot's quota. A second free project for the same use is rotation.
   - Beyond the free tier come the cascaded chain's other providers, each within its own free tier, then cheaper routes (rules and Flash-Lite before the Live model), then paid use under the daily budget.
6. **Privacy:** free tiers give the conversations to Google for improving its products.
   - In phase one the owner alone talks to the device, and makes that choice knowingly.
   - Before any other owner joins (ADR-0006), conversations move to terms that keep them out of training, such as paid tiers or each home's own API keys, or each owner consents.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Cascaded first, as Luni Cloud did | Three providers and three calls a turn, more latency, and an end of speech for Vietnamese to build. It stays, as the fallback. |
| OpenAI's Realtime API | Paid per use, with nothing like Gemini's free tier for phase one. Its plugin in LiveKit keeps it a configuration change away. |
| Vietnamese providers (FPT.AI, Viettel AI, Zalo AI) | Speech recognition and synthesis only, without a realtime model. They are candidates for the cascaded engine's voice, if Gemini's Vietnamese voices fall short, after listening. |
| Models of our own | The owner has no hardware for them. The providers' port keeps the door open once volume pays for them. |
| Unofficial endpoints (Luni Cloud's) | Excluded from releases (ADR-0001). |

## Consequences

- Positive:
  - one owner's conversations cost nothing;
  - Vietnamese and English mix naturally, in one model, with low latency;
  - the end of Vietnamese speech is solved by the model;
  - one provider's failure or quota falls back to others, through official interfaces only.
- Negative / accepted trade-offs:
  - Google may use free-tier conversations for its products; that is acceptable only while the owner is alone;
  - the first engine depends on one provider, and on models that may change: the cascaded engine and the ports bound that;
  - the free tier's limits per project are not published as a table; they are measured on the owner's project;
  - synchronous tool calls pause the model while a tool runs, so slow tools answer through a filler (Luni Cloud's idea).
- Follow-up:
  - measure the free tier's limits on the owner's project, above all the daily ones, and the Vietnamese turn's latency, end to end on the robot;
  - listen to Gemini's voices against Azure's HoaiMy and NamMinh in Vietnamese, and choose the robot's voice;
  - ADR-0004, hosting and LiveKit's media;
  - ADR-0006, before any other owner: the conversations' terms.

## References

Retrieved 2026-10-28:

- Gemini API: [pricing](https://ai.google.dev/gemini-api/docs/pricing) (updated 2026-10-27), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [Live API sessions](https://ai.google.dev/gemini-api/docs/live-session) (updated 2026-09-15), [additional terms](https://ai.google.dev/gemini-api/terms) and [their 2025-11-20 version](https://ai.google.dev/gemini-api/terms-archive/terms_11_20_25) on unpaid services.
- Google Cloud: [Live API languages and voices](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api/configure-language-voice), Vietnamese included.
- LiveKit: [Gemini Live as a realtime model](https://docs.livekit.io/agents/models/realtime/plugins/gemini/).
- Groq: free-tier limits for Whisper, as [reported by third parties](https://www.developersdigest.tech/rate-limits/groq), to confirm on [Groq's own page](https://console.groq.com/docs/rate-limits).
- Azure: [Speech pricing](https://azure.microsoft.com/pricing/details/speech/), and Vietnamese neural voices (HoaiMy, NamMinh).
