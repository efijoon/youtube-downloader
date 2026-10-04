// In-browser WebPO token minter. Bundled to src/ytdl/assets/minter.js and injected
// into a youtube.com tab, so BotGuard runs inside the real, logged-in Chrome.
import { BotGuardClient, getChallenge } from 'bgutils-js/botguard';
import { WebPoMinter } from 'bgutils-js/webpo';
import { buildURL, getHeaders } from 'bgutils-js/utils';

const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';

type State = { minter: WebPoMinter; expiresAt: number; refreshAt: number };
let state: State | null = null;
let pending: Promise<State> | null = null;

async function init(): Promise<State> {
  const challenge = await getChallenge({
    requestKey: REQUEST_KEY,
    fetchFunction: fetch.bind(globalThis),
    useYouTubeAPI: true,
  });

  let script = challenge.interpreterJavascript?.privateDoNotAccessOrElseSafeScriptWrappedValue;
  if (!script) {
    const url = challenge.interpreterUrl?.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
    if (!url) throw new Error('BotGuard interpreter unavailable');
    script = await (await fetch(url.startsWith('//') ? `https:${url}` : url)).text();
  }
  new Function(script)();

  const client = await BotGuardClient.create({
    program: challenge.program,
    globalName: challenge.globalName,
    globalObject: globalThis,
  });
  const webPoSignalOutput: any[] = [];
  const botguardResponse = await client.snapshot({ webPoSignalOutput });

  const res = await fetch(buildURL('GenerateIT', true), {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify([REQUEST_KEY, botguardResponse]),
  });
  if (!res.ok) throw new Error(`GenerateIT failed: ${res.status}`);
  const [integrityToken, ttl, refreshThreshold] = await res.json();

  const minter = await WebPoMinter.create({ integrityToken }, webPoSignalOutput);
  const now = Date.now();
  const ttlMs = (ttl ?? 43200) * 1000;
  return {
    minter,
    expiresAt: now + ttlMs,
    refreshAt: now + ttlMs - (refreshThreshold ?? 300) * 1000,
  };
}

async function getState(): Promise<State> {
  if (state && Date.now() < state.refreshAt) return state;
  pending ??= init().finally(() => { pending = null; });
  state = await pending;
  return state;
}

(globalThis as any).__ytdlMinter = {
  async mint(contentBinding: string) {
    const s = await getState();
    return { token: await s.minter.mintAsWebsafeString(contentBinding), expiresAt: Math.floor(s.expiresAt / 1000) };
  },
};
