import { mkdir, readFile, writeFile } from 'fs/promises';
import { existsSync } from 'fs';
import { join } from 'path';

export interface SavedWeixinAccount {
  accountId: string;
  rawAccountId: string;
  token: string;
  userId?: string;
  baseUrl: string;
  savedAt: string;
  contextTokens?: Record<string, string>;
}

function normalizeAccountId(raw: string): string {
  return raw.trim().replace(/[^a-zA-Z0-9._-]+/g, '-');
}

async function ensureDir(path: string): Promise<void> {
  await mkdir(path, { recursive: true });
}

function normalizeContextTokens(value: unknown): Record<string, string> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return {};
  }

  const tokens: Record<string, string> = {};
  for (const [userId, token] of Object.entries(value)) {
    const normalizedUserId = String(userId).trim();
    const normalizedToken = String(token).trim();
    if (!normalizedUserId || !normalizedToken) continue;
    tokens[normalizedUserId] = normalizedToken;
  }
  return tokens;
}

function normalizeSavedAccount(raw: unknown, fallbackAccountId = ''): SavedWeixinAccount | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return null;
  }

  const data = raw as Record<string, unknown>;
  const accountId = normalizeAccountId(String(data.accountId ?? fallbackAccountId));
  const token = String(data.token ?? '').trim();
  const baseUrl = String(data.baseUrl ?? '').trim();
  if (!accountId || !token || !baseUrl) {
    return null;
  }

  return {
    accountId,
    rawAccountId: String(data.rawAccountId ?? accountId),
    token,
    userId: data.userId ? String(data.userId) : undefined,
    baseUrl,
    savedAt: String(data.savedAt ?? new Date().toISOString()),
    contextTokens: normalizeContextTokens(data.contextTokens),
  };
}

export class WeixinAccountStore {
  constructor(private readonly authDir: string) {}

  get rootDir(): string {
    return this.authDir;
  }

  get accountsDir(): string {
    return join(this.authDir, 'accounts');
  }

  get syncDir(): string {
    return join(this.authDir, 'sync');
  }

  async init(): Promise<void> {
    await ensureDir(this.accountsDir);
    await ensureDir(this.syncDir);
  }

  accountPath(accountId: string): string {
    return join(this.accountsDir, `${accountId}.json`);
  }

  syncPath(accountId: string): string {
    return join(this.syncDir, `${accountId}.json`);
  }

  async save(
    rawAccountId: string,
    data: Omit<SavedWeixinAccount, 'accountId' | 'rawAccountId' | 'savedAt'>,
  ): Promise<SavedWeixinAccount> {
    await this.init();
    const accountId = normalizeAccountId(rawAccountId);
    const saved: SavedWeixinAccount = {
      accountId,
      rawAccountId,
      token: data.token,
      userId: data.userId,
      baseUrl: data.baseUrl,
      savedAt: new Date().toISOString(),
      contextTokens: normalizeContextTokens(data.contextTokens),
    };
    await writeFile(this.accountPath(accountId), JSON.stringify(saved, null, 2), 'utf-8');
    return saved;
  }

  async load(accountId: string): Promise<SavedWeixinAccount | null> {
    await this.init();
    const path = this.accountPath(normalizeAccountId(accountId));
    if (!existsSync(path)) return null;
    try {
      const raw = await readFile(path, 'utf-8');
      return normalizeSavedAccount(JSON.parse(raw), accountId);
    } catch {
      return null;
    }
  }

  async list(): Promise<SavedWeixinAccount[]> {
    await this.init();
    const { readdir } = await import('fs/promises');
    const files = await readdir(this.accountsDir);
    const result: SavedWeixinAccount[] = [];
    for (const file of files) {
      if (!file.endsWith('.json')) continue;
      try {
        const raw = await readFile(join(this.accountsDir, file), 'utf-8');
        const account = normalizeSavedAccount(JSON.parse(raw), file.replace(/\.json$/, ''));
        if (account) result.push(account);
      } catch {
        // Skip broken account files.
      }
    }
    return result;
  }

  async setContextToken(accountId: string, userId: string, contextToken: string): Promise<SavedWeixinAccount | null> {
    const current = await this.load(accountId);
    if (!current) return null;
    current.contextTokens = {
      ...(current.contextTokens || {}),
      [userId]: contextToken,
    };
    current.savedAt = new Date().toISOString();
    await writeFile(this.accountPath(current.accountId), JSON.stringify(current, null, 2), 'utf-8');
    return current;
  }

  async clearContextToken(accountId: string, userId: string): Promise<SavedWeixinAccount | null> {
    const current = await this.load(accountId);
    if (!current) return null;
    if (current.contextTokens) {
      delete current.contextTokens[userId];
    }
    current.savedAt = new Date().toISOString();
    await writeFile(this.accountPath(current.accountId), JSON.stringify(current, null, 2), 'utf-8');
    return current;
  }

  async loadSyncCursor(accountId: string): Promise<string> {
    const path = this.syncPath(accountId);
    if (!existsSync(path)) return '';
    try {
      const raw = await readFile(path, 'utf-8');
      const data = JSON.parse(raw) as { getUpdatesBuf?: string };
      return data.getUpdatesBuf ?? '';
    } catch {
      return '';
    }
  }

  async saveSyncCursor(accountId: string, getUpdatesBuf: string): Promise<void> {
    await this.init();
    await writeFile(this.syncPath(accountId), JSON.stringify({ getUpdatesBuf }, null, 2), 'utf-8');
  }
}
