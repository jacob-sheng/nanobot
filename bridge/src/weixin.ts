import crypto from 'crypto';
import { spawnSync } from 'child_process';
import { mkdir, readFile, writeFile } from 'fs/promises';
import { homedir } from 'os';
import { basename, extname, join } from 'path';
import qrcode from 'qrcode-terminal';
import QRCode from 'qrcode';
import { WebSocketServer, WebSocket } from 'ws';

import { WeixinAccountStore, SavedWeixinAccount } from './weixin-auth.js';
import {
  downloadAndDecryptCdnMedia,
  downloadPlainCdnMedia,
  encryptAesEcb,
  fetchQrCode,
  getUpdates,
  getUploadUrl,
  pollQrStatus,
  sendBotMessage,
  sendMessage,
  uploadEncryptedMedia,
  WeixinApiError,
  WeixinImageItem,
  WeixinMessage,
  WeixinMessageItem,
} from './weixin-api.js';

interface BridgeEvents {
  onMessage: (msg: InboundWeixinMessage) => void;
  onQR: (accountId: string | null, qr: string) => void;
  onStatus: (status: string, detail?: string, accountId?: string) => void;
}

export interface InboundWeixinMessage {
  id: string;
  accountId: string;
  sender: string;
  content: string;
  timestamp: number;
  media?: string[];
}

interface SendCommand {
  type: 'send';
  to: string;
  text: string;
  media?: string[];
}

interface BridgeHeartbeat {
  type: 'heartbeat';
  timestamp: number;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractText(itemList?: WeixinMessageItem[]): string {
  if (!itemList?.length) return '';
  for (const item of itemList) {
    if (item.type === 1 && item.text_item?.text) return item.text_item.text;
    if (item.type === 3 && item.voice_item?.text) return item.voice_item.text;
  }
  return '';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function sanitizePathPart(value: string): string {
  return value.replace(/[^a-zA-Z0-9_.-]+/g, '_').slice(0, 80) || 'msg';
}

function guessImageExtensionFromBytes(data: Buffer): string {
  if (data.length >= 3 && data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff) return '.jpg';
  if (
    data.length >= 8 &&
    data[0] === 0x89 &&
    data[1] === 0x50 &&
    data[2] === 0x4e &&
    data[3] === 0x47 &&
    data[4] === 0x0d &&
    data[5] === 0x0a &&
    data[6] === 0x1a &&
    data[7] === 0x0a
  ) {
    return '.png';
  }
  if (data.length >= 6 && (data.subarray(0, 6).toString('ascii') === 'GIF87a' || data.subarray(0, 6).toString('ascii') === 'GIF89a')) {
    return '.gif';
  }
  if (data.length >= 12 && data.subarray(0, 4).toString('ascii') === 'RIFF' && data.subarray(8, 12).toString('ascii') === 'WEBP') {
    return '.webp';
  }
  if (data.length >= 2 && data[0] === 0x42 && data[1] === 0x4d) return '.bmp';
  if (data.length >= 12 && data.subarray(4, 8).toString('ascii') === 'ftyp') {
    const brand = data.subarray(8, 12).toString('ascii').toLowerCase();
    if (brand.includes('heic') || brand.includes('heix') || brand.includes('mif1') || brand.includes('msf1')) {
      return '.heic';
    }
  }
  return '.jpg';
}

export class WeixinBridgeServer {
  private static readonly HEARTBEAT_INTERVAL_MS = 15000;
  private static readonly SESSION_EXPIRED_ERRCODE = -14;
  private static readonly SESSION_PAUSE_MS = 60 * 60 * 1000;
  private wss: WebSocketServer | null = null;
  private clients = new Set<WebSocket>();
  private store: WeixinAccountStore;
  private accounts = new Map<string, SavedWeixinAccount>();
  private contextTokens = new Map<string, string>();
  private monitorTasks = new Map<string, Promise<void>>();
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private sessionPauseUntil = new Map<string, number>();
  private stopped = false;

  constructor(
    private readonly port: number,
    authDir: string,
    private readonly baseUrl: string,
    private readonly loginMode: boolean,
    private readonly token?: string,
    private readonly configPath?: string,
    private readonly qrPath?: string,
    private readonly nanobotBin?: string,
    private readonly gatewayService?: string,
    private readonly routeTag?: string,
    private readonly cdnBaseUrl = 'https://novac2c.cdn.weixin.qq.com/c2c',
  ) {
    this.store = new WeixinAccountStore(authDir);
  }

  async start(): Promise<void> {
    await this.store.init();
    this.wss = new WebSocketServer({ host: '127.0.0.1', port: this.port });
    this.logInfo('bridge.start', { url: `ws://127.0.0.1:${this.port}` });
    if (this.token) this.logInfo('bridge.auth_enabled');
    this.startHeartbeatLoop();

    this.wss.on('connection', (ws) => {
      this.logInfo('bridge.client_connecting', { remote: this.describeRemote(ws) });
      if (this.token) {
        const timeout = setTimeout(() => ws.close(4001, 'Auth timeout'), 5000);
        ws.once('message', (data) => {
          clearTimeout(timeout);
          try {
            const msg = JSON.parse(data.toString()) as { type?: string; token?: string };
            if (msg.type === 'auth' && msg.token === this.token) {
              this.setupClient(ws);
            } else {
              ws.close(4003, 'Invalid token');
            }
          } catch {
            ws.close(4003, 'Invalid auth message');
          }
        });
      } else {
        this.setupClient(ws);
      }
    });

    let saved = await this.store.list();
    if (this.loginMode || saved.length === 0) {
      const account = await this.loginViaQr();
      saved = [account, ...saved.filter((item) => item.accountId !== account.accountId)];
    }

    for (const account of saved) {
      this.accounts.set(account.accountId, account);
      for (const [userId, contextToken] of Object.entries(account.contextTokens || {})) {
        this.contextTokens.set(this.contextKey(account.accountId, userId), contextToken);
      }
      this.startMonitor(account);
    }
  }

  private contextKey(accountId: string, userId: string): string {
    return `${accountId}:${userId}`;
  }

  private setupClient(ws: WebSocket): void {
    this.clients.add(ws);
    this.logInfo('bridge.client_connected', { clients: this.clients.size });
    this.sendHeartbeat(ws);

    ws.on('message', async (data) => {
      try {
        const cmd = JSON.parse(data.toString()) as SendCommand;
        if (cmd.type === 'send') {
          await this.handleSend(cmd);
          ws.send(JSON.stringify({ type: 'sent', to: cmd.to }));
        }
      } catch (error) {
        this.logError('bridge.client_command_failed', { error: this.errorDetail(error) });
        ws.send(JSON.stringify({ type: 'error', error: String(error) }));
      }
    });

    ws.on('close', (code, reason) => {
      this.clients.delete(ws);
      this.logWarning('bridge.client_closed', {
        clients: this.clients.size,
        code,
        reason: reason.toString(),
      });
    });
    ws.on('error', (error) => {
      this.clients.delete(ws);
      this.logWarning('bridge.client_error', {
        clients: this.clients.size,
        error: this.errorDetail(error),
      });
    });
  }

  private broadcast(payload: Record<string, unknown>): void {
    const data = JSON.stringify(payload);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data);
      }
    }
  }

  private async loginViaQr(): Promise<SavedWeixinAccount> {
    this.logInfo('weixin.login_start');
    const initial = await fetchQrCode(this.baseUrl);
    this.broadcast({ type: 'qr', qr: initial.qrcodeUrl });
    await this.writeQrPng(initial.qrcodeUrl);
    qrcode.generate(initial.qrcodeUrl, { small: true });
    this.logInfo('weixin.login_waiting_for_scan');

    let currentQr = initial.qrcode;
    let refreshes = 0;
    while (!this.stopped) {
      const result = await pollQrStatus(this.baseUrl, currentQr);
      if (result.status === 'scaned') {
        this.logInfo('weixin.login_scanned');
      }
      if (result.status === 'expired') {
        refreshes += 1;
        if (refreshes > 3) {
          throw new Error('二维码多次过期，请重新运行登录命令');
        }
        this.logWarning('weixin.login_qr_expired', { refreshes, maxRefreshes: 3 });
        const next = await fetchQrCode(this.baseUrl);
        currentQr = next.qrcode;
        this.broadcast({ type: 'qr', qr: next.qrcodeUrl });
        await this.writeQrPng(next.qrcodeUrl);
        qrcode.generate(next.qrcodeUrl, { small: true });
        continue;
      }
      if (result.status === 'confirmed' && result.bot_token && result.ilink_bot_id) {
        const account = await this.store.save(result.ilink_bot_id, {
          token: result.bot_token,
          userId: result.ilink_user_id,
          baseUrl: result.baseurl || this.baseUrl,
        });
        if (result.ilink_user_id) {
          try {
            this.finalizeLogin(account, result.ilink_user_id);
            this.logInfo('weixin.login_finalized', {
              gatewayService: this.gatewayService || 'nanobot-gateway.service',
            });
          } catch (error) {
            this.logError('weixin.login_finalize_failed', { error: this.errorDetail(error) });
          }
        } else {
          this.logError('weixin.login_missing_user_id');
        }
        this.logInfo('weixin.login_confirmed', { accountId: account.accountId });
        this.broadcast({
          type: 'status',
          status: 'connected',
          accountId: account.accountId,
          detail: 'login_confirmed',
        });
        return account;
      }
      await sleep(1200);
    }
    throw new Error('login aborted');
  }

  private async writeQrPng(qrContent: string): Promise<void> {
    if (!this.qrPath) return;
    await QRCode.toFile(this.qrPath, qrContent, {
      type: 'png',
      margin: 2,
      width: 512,
      color: {
        dark: '#111111',
        light: '#ffffff',
      },
    });
    this.logInfo('weixin.qr_written', { path: this.qrPath });
  }

  private finalizeLogin(account: SavedWeixinAccount, userId: string): void {
    if (!this.nanobotBin) {
      throw new Error('WEIXIN_NANOBOT_BIN is not set');
    }
    if (!this.configPath) {
      throw new Error('WEIXIN_CONFIG_PATH is not set');
    }

    const result = spawnSync(
      this.nanobotBin,
      [
        'weixin',
        'finalize-login',
        '--config',
        this.configPath,
        '--user-id',
        userId,
        '--base-url',
        account.baseUrl,
        '--state-dir',
        this.store.rootDir,
        '--gateway-service',
        this.gatewayService || 'nanobot-gateway.service',
      ],
      {
        stdio: 'inherit',
      },
    );
    if (result.status !== 0) {
      throw new Error(`finalize-login exited with status ${result.status}`);
    }
  }

  private async monitorAccount(account: SavedWeixinAccount): Promise<void> {
    this.broadcast({ type: 'status', status: 'connected', accountId: account.accountId, detail: 'monitor_started' });
    this.logInfo('weixin.monitor_started', { accountId: account.accountId });
    let cursor = await this.store.loadSyncCursor(account.accountId);
    let timeoutMs = 35000;

    while (!this.stopped) {
      const pauseUntil = this.sessionPauseUntil.get(account.accountId) || 0;
      if (pauseUntil > Date.now()) {
        const waitMs = pauseUntil - Date.now();
        this.logWarning('weixin.monitor_session_paused', {
          accountId: account.accountId,
          waitSeconds: Math.ceil(waitMs / 1000),
        });
        await sleep(waitMs);
        continue;
      }
      try {
        const response = await getUpdates(account.baseUrl, account.token, cursor, timeoutMs, this.routeTag);
        if (response.get_updates_buf) {
          cursor = response.get_updates_buf;
          await this.store.saveSyncCursor(account.accountId, cursor);
        }
        if (response.longpolling_timeout_ms && response.longpolling_timeout_ms > 0) {
          timeoutMs = response.longpolling_timeout_ms;
        }
        if ((response.ret ?? 0) !== 0 || (response.errcode ?? 0) !== 0) {
          const errcode = response.errcode ?? 0;
          const ret = response.ret ?? 0;
          const detail = `${response.errmsg || errcode || ret}`;
          if (errcode === WeixinBridgeServer.SESSION_EXPIRED_ERRCODE || ret === WeixinBridgeServer.SESSION_EXPIRED_ERRCODE) {
            this.sessionPauseUntil.set(account.accountId, Date.now() + WeixinBridgeServer.SESSION_PAUSE_MS);
            this.logWarning('weixin.monitor_session_expired', { accountId: account.accountId, errcode, ret, detail });
            this.broadcast({
              type: 'status',
              status: 'disconnected',
              accountId: account.accountId,
              detail: 'session_expired',
            });
            continue;
          }
          this.logWarning('weixin.monitor_api_error', { accountId: account.accountId, errcode, ret, detail });
          this.broadcast({
            type: 'error',
            error: `weixin getupdates failed for ${account.accountId}: ${detail}`,
          });
          await sleep(2000);
          continue;
        }

        for (const msg of response.msgs ?? []) {
          await this.handleInboundMessage(account, msg);
        }
      } catch (error) {
        const event = this.classifyMonitorError(error);
        this.logWarning(event, {
          accountId: account.accountId,
          error: this.errorDetail(error),
        });
        this.broadcast({
          type: 'status',
          status: 'disconnected',
          accountId: account.accountId,
          detail: event,
        });
        await sleep(5000);
      }
    }
    this.logInfo('weixin.monitor_stopped', { accountId: account.accountId });
  }

  private classifyMonitorError(error: unknown): string {
    if (error instanceof WeixinApiError) {
      if (error.errcode === WeixinBridgeServer.SESSION_EXPIRED_ERRCODE || error.ret === WeixinBridgeServer.SESSION_EXPIRED_ERRCODE) {
        return 'weixin.monitor_session_expired';
      }
      return 'weixin.monitor_api_error';
    }
    const detail = this.errorDetail(error).toLowerCase();
    if (/fetch failed|econn|etimedout|network|socket|connect|abort/.test(detail)) {
      return 'weixin.monitor_network_error';
    }
    return 'weixin.monitor_error';
  }

  private summarizeItemTypes(msg: WeixinMessage): string {
    const types = (msg.item_list ?? [])
      .map((item) => String(item.type ?? '?'))
      .join(',');
    return types || 'none';
  }

  private normalizeInboundImageItems(msg: WeixinMessage): WeixinMessageItem[] {
    const items = [...(msg.item_list ?? [])];
    if (msg.image_item) {
      items.push({ type: 2, image_item: msg.image_item });
    }
    return items.filter((item) => item.type === 2 && isRecord(item.image_item));
  }

  private imageHasEncryptedMedia(image: WeixinImageItem | undefined): boolean {
    return Boolean(String(image?.media?.encrypt_query_param || '').trim());
  }

  private imageHasAesKey(image: WeixinImageItem | undefined): boolean {
    return Boolean(String(image?.aeskey || image?.media?.aes_key || '').trim());
  }

  private mediaDir(): string {
    return join(process.env.HOME || homedir(), '.nanobot', 'media', 'weixin');
  }

  private async downloadInboundImages(
    account: SavedWeixinAccount,
    sender: string,
    msg: WeixinMessage,
  ): Promise<string[]> {
    const imageItems = this.normalizeInboundImageItems(msg);
    if (!imageItems.length) return [];

    this.logInfo('weixin.inbound_item_summary', {
      accountId: account.accountId,
      sender,
      messageType: msg.message_type ?? null,
      itemTypes: this.summarizeItemTypes(msg),
      imageItems: imageItems.length,
    });
    await mkdir(this.mediaDir(), { recursive: true });

    const saved: string[] = [];
    const stamp = sanitizePathPart(String(msg.message_id ?? msg.client_id ?? Date.now()));
    const accountPart = sanitizePathPart(account.accountId);
    const senderPart = sanitizePathPart(sender);

    for (const [index, item] of imageItems.entries()) {
      const image = item.image_item;
      const encryptedQueryParam = String(image?.media?.encrypt_query_param || '').trim();
      if (!encryptedQueryParam) {
        this.logWarning('weixin.image_missing_encrypt_query_param', {
          accountId: account.accountId,
          sender,
          index: index + 1,
          itemTypes: this.summarizeItemTypes(msg),
        });
        continue;
      }

      try {
        const aesKeyBase64 = image?.aeskey
          ? Buffer.from(String(image.aeskey), 'hex').toString('base64')
          : String(image?.media?.aes_key || '').trim() || undefined;
        this.logInfo('weixin.image_download_start', {
          accountId: account.accountId,
          sender,
          index: index + 1,
          hasEncryptQueryParam: true,
          hasAesKey: Boolean(aesKeyBase64),
        });
        const data = aesKeyBase64
          ? await downloadAndDecryptCdnMedia(encryptedQueryParam, aesKeyBase64, 20000, this.cdnBaseUrl)
          : await downloadPlainCdnMedia(encryptedQueryParam, 20000, this.cdnBaseUrl);
        const ext = guessImageExtensionFromBytes(data);
        const filename = `${accountPart}_${senderPart}_${stamp}_${index + 1}${ext}`;
        const fullPath = join(this.mediaDir(), filename);
        await writeFile(fullPath, data);
        this.logInfo('weixin.image_saved', {
          accountId: account.accountId,
          sender,
          index: index + 1,
          path: fullPath,
          bytes: data.length,
        });
        saved.push(fullPath);
      } catch (error) {
        this.logWarning('weixin.image_download_failed', {
          accountId: account.accountId,
          sender,
          index: index + 1,
          hasAesKey: this.imageHasAesKey(image),
          error: this.errorDetail(error),
        });
      }
    }

    return saved;
  }

  private async handleInboundMessage(account: SavedWeixinAccount, msg: WeixinMessage): Promise<void> {
    const sender = (msg.from_user_id || '').trim();
    if (!sender) return;

    const content = extractText(msg.item_list);
    const media = await this.downloadInboundImages(account, sender, msg);
    const placeholders = media.map((path) => `[image: ${basename(path)}]`);
    const finalContent = [content, ...placeholders].filter(Boolean).join('\n').trim();
    if (!finalContent && media.length === 0) return;

    if (msg.context_token) {
      this.contextTokens.set(this.contextKey(account.accountId, sender), msg.context_token);
      const updated = await this.store.setContextToken(account.accountId, sender, msg.context_token);
      if (updated) {
        this.accounts.set(account.accountId, updated);
      }
    }

    if (media.length) {
      this.logInfo('weixin.forwarding_media', { accountId: account.accountId, sender, count: media.length });
    }
    this.broadcast({
      type: 'message',
      id: String(msg.message_id ?? msg.client_id ?? `${Date.now()}`),
      accountId: account.accountId,
      sender,
      content: finalContent,
      timestamp: msg.create_time_ms ?? Date.now(),
      media,
    });
  }

  private async handleSend(cmd: SendCommand): Promise<void> {
    const sepIndex = cmd.to.indexOf('|');
    if (sepIndex === -1) {
      throw new Error(`Invalid Weixin recipient: ${cmd.to}`);
    }

    const accountId = cmd.to.slice(0, sepIndex);
    const toUserId = cmd.to.slice(sepIndex + 1);
    const account = this.accounts.get(accountId);
    if (!account) {
      throw new Error(`Unknown Weixin account: ${accountId}`);
    }

    const contextToken = this.contextTokens.get(this.contextKey(accountId, toUserId));
    if (!contextToken) {
      throw new Error(`No Weixin context token cached for ${toUserId} on ${accountId}`);
    }

    const media = (cmd.media || []).map((item) => String(item).trim()).filter(Boolean);
    const failedMedia: string[] = [];

    for (const mediaPath of media) {
      try {
        await this.sendMediaFile(account, toUserId, mediaPath, contextToken);
      } catch (error) {
        const event = await this.handleSendFailure(accountId, toUserId, error);
        this.logWarning(event, {
          accountId,
          toUserId,
          mediaPath,
          error: this.errorDetail(error),
        });
        failedMedia.push(basename(mediaPath));
        if (event === 'weixin.send_invalid_context_token' || event === 'weixin.send_session_expired') {
          throw error;
        }
      }
    }

    if (cmd.text.trim()) {
      this.logInfo('weixin.send_attempt', { accountId, toUserId });
      await sendMessage(account.baseUrl, account.token, toUserId, cmd.text, contextToken, this.routeTag);
      this.logInfo('weixin.send_success', { accountId, toUserId });
    }

    if (failedMedia.length) {
      const note = `[Media send failed: ${failedMedia.join(', ')}]`;
      await sendMessage(account.baseUrl, account.token, toUserId, note, contextToken, this.routeTag);
      this.logWarning('weixin.send_media_partial_failure', {
        accountId,
        toUserId,
        count: failedMedia.length,
      });
    }
  }

  private async handleSendFailure(accountId: string, toUserId: string, error: unknown): Promise<string> {
    if (error instanceof WeixinApiError) {
      if (error.errcode === WeixinBridgeServer.SESSION_EXPIRED_ERRCODE || error.ret === WeixinBridgeServer.SESSION_EXPIRED_ERRCODE) {
        this.sessionPauseUntil.set(accountId, Date.now() + WeixinBridgeServer.SESSION_PAUSE_MS);
        return 'weixin.send_session_expired';
      }
      const detail = `${error.errmsg || error.message}`.toLowerCase();
      if (/agent session|context token|context_token|invalid session/.test(detail)) {
        this.contextTokens.delete(this.contextKey(accountId, toUserId));
        const updated = await this.store.clearContextToken(accountId, toUserId);
        if (updated) {
          this.accounts.set(accountId, updated);
        }
        return 'weixin.send_invalid_context_token';
      }
      return 'weixin.send_api_error';
    }

    if (/no weixin context token cached/i.test(this.errorDetail(error))) {
      return 'weixin.send_missing_context_token';
    }
    return 'weixin.send_error';
  }

  private async sendMediaFile(
    account: SavedWeixinAccount,
    toUserId: string,
    mediaPath: string,
    contextToken: string,
  ): Promise<void> {
    const rawData = await readFile(mediaPath);
    const fileSize = rawData.length;
    const fileMd5 = crypto.createHash('md5').update(rawData).digest('hex');
    const fileKey = crypto.randomBytes(16).toString('hex');
    const aesKeyRaw = crypto.randomBytes(16);
    const aesKeyHex = aesKeyRaw.toString('hex');
    const encryptedData = encryptAesEcb(rawData, aesKeyRaw);
    const paddedSize = Math.ceil((fileSize + 1) / 16) * 16;
    const ext = extname(mediaPath).toLowerCase();

    let mediaType = 3;
    let itemType = 4;
    let itemKey = 'file_item';
    if (['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.ico', '.svg', '.heic'].includes(ext)) {
      mediaType = 1;
      itemType = 2;
      itemKey = 'image_item';
    } else if (['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv'].includes(ext)) {
      mediaType = 2;
      itemType = 5;
      itemKey = 'video_item';
    }

    const upload = await getUploadUrl(
      account.baseUrl,
      account.token,
      {
        filekey: fileKey,
        media_type: mediaType,
        to_user_id: toUserId,
        rawsize: fileSize,
        rawfilemd5: fileMd5,
        filesize: paddedSize,
        no_need_thumb: true,
        aeskey: aesKeyHex,
      },
      this.routeTag,
    );
    const downloadParam = await uploadEncryptedMedia(upload.upload_param, fileKey, encryptedData, this.cdnBaseUrl);
    const cdnAesKeyB64 = Buffer.from(aesKeyHex, 'utf-8').toString('base64');

    const mediaItem: Record<string, unknown> = {
      media: {
        encrypt_query_param: downloadParam,
        aes_key: cdnAesKeyB64,
        encrypt_type: 1,
      },
    };
    if (itemType === 2) {
      mediaItem.mid_size = paddedSize;
    } else if (itemType === 5) {
      mediaItem.video_size = paddedSize;
    } else {
      mediaItem.file_name = basename(mediaPath);
      mediaItem.len = String(fileSize);
    }

    await sendBotMessage(
      account.baseUrl,
      account.token,
      {
        from_user_id: '',
        to_user_id: toUserId,
        client_id: `nanobot-${Date.now()}-${fileKey.slice(0, 8)}`,
        message_type: 2,
        message_state: 2,
        context_token: contextToken,
        item_list: [{ type: itemType, [itemKey]: mediaItem }],
      },
      this.routeTag,
    );
    this.logInfo('weixin.send_media_success', {
      accountId: account.accountId,
      toUserId,
      mediaPath,
      itemKey,
    });
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();
    await Promise.allSettled([...this.monitorTasks.values()]);
    this.monitorTasks.clear();
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }
    this.logInfo('bridge.stopped');
  }

  private startHeartbeatLoop(): void {
    this.heartbeatTimer = setInterval(() => {
      const payload: BridgeHeartbeat = { type: 'heartbeat', timestamp: Date.now() };
      this.broadcast(payload as unknown as Record<string, unknown>);
    }, WeixinBridgeServer.HEARTBEAT_INTERVAL_MS);
  }

  private sendHeartbeat(client: WebSocket): void {
    if (client.readyState !== WebSocket.OPEN) return;
    const payload: BridgeHeartbeat = { type: 'heartbeat', timestamp: Date.now() };
    client.send(JSON.stringify(payload));
  }

  private startMonitor(account: SavedWeixinAccount): void {
    const task = this.monitorAccount(account)
      .catch((error) => {
        this.logError('weixin.monitor_crashed', {
          accountId: account.accountId,
          error: this.errorDetail(error),
        });
        if (!this.stopped) {
          setTimeout(() => this.startMonitor(account), 5000);
        }
      })
      .finally(() => {
        if (this.monitorTasks.get(account.accountId) === task) {
          this.monitorTasks.delete(account.accountId);
        }
      });
    this.monitorTasks.set(account.accountId, task);
  }

  private describeRemote(ws: WebSocket): string {
    const socket = (ws as WebSocket & { _socket?: { remoteAddress?: string; remotePort?: number } })._socket;
    const address = socket?.remoteAddress || 'unknown';
    const port = socket?.remotePort || 'unknown';
    return `${address}:${port}`;
  }

  private errorDetail(error: unknown): string {
    if (error instanceof Error) return `${error.name}: ${error.message}`;
    return String(error);
  }

  private logInfo(event: string, fields?: Record<string, unknown>): void {
    this.log('INFO', event, fields);
  }

  private logWarning(event: string, fields?: Record<string, unknown>): void {
    this.log('WARN', event, fields);
  }

  private logError(event: string, fields?: Record<string, unknown>): void {
    this.log('ERROR', event, fields);
  }

  private log(level: string, event: string, fields?: Record<string, unknown>): void {
    const suffix = fields && Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : '';
    console.log(`${level} ${event}${suffix}`);
  }
}
