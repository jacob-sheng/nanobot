import crypto from 'crypto';

export interface WeixinMessageItem {
  type?: number;
  text_item?: { text?: string };
  voice_item?: { text?: string };
  image_item?: Record<string, unknown>;
  file_item?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface WeixinMessage {
  message_id?: number;
  client_id?: string;
  from_user_id?: string;
  create_time_ms?: number;
  message_type?: number;
  item_list?: WeixinMessageItem[];
  context_token?: string;
  image_item?: Record<string, unknown>;
  file_item?: Record<string, unknown>;
  [key: string]: unknown;
}

interface StatusResponse {
  status: 'wait' | 'scaned' | 'confirmed' | 'expired';
  bot_token?: string;
  ilink_bot_id?: string;
  baseurl?: string;
  ilink_user_id?: string;
}

interface GetUpdatesResponse {
  ret?: number;
  errcode?: number;
  errmsg?: string;
  msgs?: WeixinMessage[];
  get_updates_buf?: string;
  longpolling_timeout_ms?: number;
}

function ensureTrailingSlash(url: string): string {
  return url.endsWith('/') ? url : `${url}/`;
}

function randomWechatUin(): string {
  const uint32 = crypto.randomBytes(4).readUInt32BE(0);
  return Buffer.from(String(uint32), 'utf-8').toString('base64');
}

function buildHeaders(body: string, token?: string): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...buildAuthHeaders(token),
    'Content-Length': String(Buffer.byteLength(body, 'utf-8')),
  };
  return headers;
}

function buildAuthHeaders(token?: string): Record<string, string> {
  const headers: Record<string, string> = {
    AuthorizationType: 'ilink_bot_token',
    'X-WECHAT-UIN': randomWechatUin(),
  };
  if (token?.trim()) {
    headers.Authorization = `Bearer ${token.trim()}`;
  }
  return headers;
}

async function postJson<T>(baseUrl: string, endpoint: string, body: Record<string, unknown>, token?: string, timeoutMs = 15000): Promise<T> {
  const url = new URL(endpoint, ensureTrailingSlash(baseUrl));
  const payload = JSON.stringify({ ...body, base_info: { channel_version: 'nanobot-weixin-bridge' } });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url.toString(), {
      method: 'POST',
      headers: buildHeaders(payload, token),
      body: payload,
      signal: controller.signal,
    });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`${endpoint} ${response.status}: ${text}`);
    }
    return JSON.parse(text) as T;
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchQrCode(baseUrl: string, botType = '3'): Promise<{ qrcode: string; qrcodeUrl: string }> {
  const url = new URL(`ilink/bot/get_bot_qrcode?bot_type=${encodeURIComponent(botType)}`, ensureTrailingSlash(baseUrl));
  const response = await fetch(url.toString());
  if (!response.ok) {
    throw new Error(`get_bot_qrcode ${response.status}: ${await response.text()}`);
  }
  const data = await response.json() as { qrcode: string; qrcode_img_content: string };
  return { qrcode: data.qrcode, qrcodeUrl: data.qrcode_img_content };
}

export async function pollQrStatus(baseUrl: string, qrcode: string, timeoutMs = 35000): Promise<StatusResponse> {
  const url = new URL(`ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(qrcode)}`, ensureTrailingSlash(baseUrl));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url.toString(), {
      headers: { 'iLink-App-ClientVersion': '1' },
      signal: controller.signal,
    });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`get_qrcode_status ${response.status}: ${text}`);
    }
    return JSON.parse(text) as StatusResponse;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return { status: 'wait' };
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function getUpdates(baseUrl: string, token: string, getUpdatesBuf: string, timeoutMs = 35000): Promise<GetUpdatesResponse> {
  try {
    return await postJson<GetUpdatesResponse>(
      baseUrl,
      'ilink/bot/getupdates',
      { get_updates_buf: getUpdatesBuf },
      token,
      timeoutMs,
    );
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return { ret: 0, msgs: [], get_updates_buf: getUpdatesBuf };
    }
    throw error;
  }
}

export async function sendMessage(baseUrl: string, token: string, toUserId: string, text: string, contextToken: string): Promise<void> {
  await postJson(
    baseUrl,
    'ilink/bot/sendmessage',
    {
      msg: {
        from_user_id: '',
        to_user_id: toUserId,
        client_id: `nanobot-${Date.now()}`,
        message_type: 2,
        message_state: 2,
        context_token: contextToken,
        item_list: [{ type: 1, text_item: { text } }],
      },
    },
    token,
  );
}

export async function downloadMedia(url: string, token?: string, timeoutMs = 20000): Promise<{ data: Buffer; contentType: string | null }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: buildAuthHeaders(token),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`download ${response.status}: ${await response.text()}`);
    }
    const arrayBuffer = await response.arrayBuffer();
    return {
      data: Buffer.from(arrayBuffer),
      contentType: response.headers.get('content-type'),
    };
  } finally {
    clearTimeout(timer);
  }
}
