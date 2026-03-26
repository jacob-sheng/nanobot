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

interface WeixinApiResponse {
  ret?: number;
  errcode?: number;
  errmsg?: string;
  [key: string]: unknown;
}

export class WeixinApiError extends Error {
  constructor(
    readonly endpoint: string,
    readonly errcode?: number,
    readonly ret?: number,
    readonly errmsg?: string,
    readonly statusCode?: number,
  ) {
    const pieces = [endpoint];
    if (statusCode) pieces.push(`status=${statusCode}`);
    if (typeof ret === 'number') pieces.push(`ret=${ret}`);
    if (typeof errcode === 'number') pieces.push(`errcode=${errcode}`);
    if (errmsg) pieces.push(`errmsg=${errmsg}`);
    super(pieces.join(' '));
    this.name = 'WeixinApiError';
  }
}

const WEIXIN_CHANNEL_VERSION = '1.0.3';
const BASE_INFO = { channel_version: WEIXIN_CHANNEL_VERSION };
const DEFAULT_CDN_BASE_URL = 'https://novac2c.cdn.weixin.qq.com/c2c';

function ensureTrailingSlash(url: string): string {
  return url.endsWith('/') ? url : `${url}/`;
}

function randomWechatUin(): string {
  const uint32 = crypto.randomBytes(4).readUInt32BE(0);
  return Buffer.from(String(uint32), 'utf-8').toString('base64');
}

function buildHeaders(body: string, token?: string, routeTag?: string): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...buildAuthHeaders(token, routeTag),
    'Content-Length': String(Buffer.byteLength(body, 'utf-8')),
  };
  return headers;
}

function buildAuthHeaders(token?: string, routeTag?: string): Record<string, string> {
  const headers: Record<string, string> = {
    AuthorizationType: 'ilink_bot_token',
    'X-WECHAT-UIN': randomWechatUin(),
  };
  if (token?.trim()) {
    headers.Authorization = `Bearer ${token.trim()}`;
  }
  if (routeTag?.trim()) {
    headers.SKRouteTag = routeTag.trim();
  }
  return headers;
}

function parseApiPayload(text: string): WeixinApiResponse | null {
  if (!text.trim()) return {};
  try {
    return JSON.parse(text) as WeixinApiResponse;
  } catch {
    return null;
  }
}

function ensureApiSuccess(endpoint: string, payload: WeixinApiResponse): void {
  const ret = typeof payload.ret === 'number' ? payload.ret : 0;
  const errcode = typeof payload.errcode === 'number' ? payload.errcode : 0;
  if (ret === 0 && errcode === 0) return;
  throw new WeixinApiError(endpoint, errcode || undefined, ret || undefined, String(payload.errmsg || '').trim() || undefined);
}

async function postJson<T>(
  baseUrl: string,
  endpoint: string,
  body: Record<string, unknown>,
  token?: string,
  timeoutMs = 15000,
  routeTag?: string,
): Promise<T> {
  const url = new URL(endpoint, ensureTrailingSlash(baseUrl));
  const payload = JSON.stringify({ ...body, base_info: BASE_INFO });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url.toString(), {
      method: 'POST',
      headers: buildHeaders(payload, token, routeTag),
      body: payload,
      signal: controller.signal,
    });
    const text = await response.text();
    const parsed = parseApiPayload(text);
    if (!response.ok) {
      if (parsed) {
        throw new WeixinApiError(
          endpoint,
          typeof parsed.errcode === 'number' ? parsed.errcode : undefined,
          typeof parsed.ret === 'number' ? parsed.ret : undefined,
          String(parsed.errmsg || '').trim() || undefined,
          response.status,
        );
      }
      throw new WeixinApiError(endpoint, undefined, undefined, text.trim() || undefined, response.status);
    }
    if (!parsed) {
      throw new Error(`${endpoint} returned non-JSON payload`);
    }
    return parsed as T;
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

export async function getUpdates(
  baseUrl: string,
  token: string,
  getUpdatesBuf: string,
  timeoutMs = 35000,
  routeTag?: string,
): Promise<GetUpdatesResponse> {
  try {
    return await postJson<GetUpdatesResponse>(
      baseUrl,
      'ilink/bot/getupdates',
      { get_updates_buf: getUpdatesBuf },
      token,
      timeoutMs,
      routeTag,
    );
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return { ret: 0, msgs: [], get_updates_buf: getUpdatesBuf };
    }
    throw error;
  }
}

export async function sendBotMessage(
  baseUrl: string,
  token: string,
  msg: Record<string, unknown>,
  routeTag?: string,
): Promise<void> {
  const response = await postJson<WeixinApiResponse>(
    baseUrl,
    'ilink/bot/sendmessage',
    { msg },
    token,
    15000,
    routeTag,
  );
  ensureApiSuccess('ilink/bot/sendmessage', response);
}

export async function sendMessage(
  baseUrl: string,
  token: string,
  toUserId: string,
  text: string,
  contextToken: string,
  routeTag?: string,
): Promise<void> {
  await sendBotMessage(
    baseUrl,
    token,
    {
      from_user_id: '',
      to_user_id: toUserId,
      client_id: `nanobot-${Date.now()}`,
      message_type: 2,
      message_state: 2,
      context_token: contextToken,
      item_list: [{ type: 1, text_item: { text } }],
    },
    routeTag,
  );
}

export async function getUploadUrl(
  baseUrl: string,
  token: string,
  uploadBody: Record<string, unknown>,
  routeTag?: string,
): Promise<{ upload_param: string }> {
  const response = await postJson<WeixinApiResponse & { upload_param?: string }>(
    baseUrl,
    'ilink/bot/getuploadurl',
    uploadBody,
    token,
    15000,
    routeTag,
  );
  ensureApiSuccess('ilink/bot/getuploadurl', response);
  return { upload_param: String(response.upload_param || '') };
}

export function encryptAesEcb(data: Buffer, key: Buffer): Buffer {
  const cipher = crypto.createCipheriv('aes-128-ecb', key, null);
  cipher.setAutoPadding(true);
  return Buffer.concat([cipher.update(data), cipher.final()]);
}

export async function uploadEncryptedMedia(
  uploadParam: string,
  fileKey: string,
  encryptedData: Buffer,
  cdnBaseUrl = DEFAULT_CDN_BASE_URL,
): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const uploadUrl = `${cdnBaseUrl}/upload?encrypted_query_param=${encodeURIComponent(uploadParam)}&filekey=${encodeURIComponent(fileKey)}`;
    const response = await fetch(uploadUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: new Uint8Array(encryptedData),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new WeixinApiError('cdn/upload', undefined, undefined, await response.text(), response.status);
    }
    const encryptedParam = response.headers.get('x-encrypted-param');
    if (!encryptedParam) {
      throw new Error('CDN upload response missing x-encrypted-param');
    }
    return encryptedParam;
  } finally {
    clearTimeout(timer);
  }
}

export async function downloadMedia(
  url: string,
  token?: string,
  timeoutMs = 20000,
  routeTag?: string,
): Promise<{ data: Buffer; contentType: string | null }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: buildAuthHeaders(token, routeTag),
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
