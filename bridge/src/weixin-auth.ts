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
}

function normalizeAccountId(raw: string): string {
  return raw.trim().replace(/[^a-zA-Z0-9._-]+/g, '-');
}

async function ensureDir(path: string): Promise<void> {
  await mkdir(path, { recursive: true });
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

  async save(rawAccountId: string, data: Omit<SavedWeixinAccount, 'accountId' | 'rawAccountId' | 'savedAt'>): Promise<SavedWeixinAccount> {
    await this.init();
    const accountId = normalizeAccountId(rawAccountId);
    const saved: SavedWeixinAccount = {
      accountId,
      rawAccountId,
      token: data.token,
      userId: data.userId,
      baseUrl: data.baseUrl,
      savedAt: new Date().toISOString(),
    };
    await writeFile(this.accountPath(accountId), JSON.stringify(saved, null, 2), 'utf-8');
    return saved;
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
        result.push(JSON.parse(raw) as SavedWeixinAccount);
      } catch {
        // Skip broken account files.
      }
    }
    return result;
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
