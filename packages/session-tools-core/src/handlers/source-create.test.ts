import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import { mkdtempSync, rmSync, existsSync, readFileSync, mkdirSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { SessionToolContext } from '../context.ts';
import { handleSourceCreate } from './source-create.ts';

function createTestContext(workspacePath: string): SessionToolContext {
  const sourcesPath = join(workspacePath, 'sources');

  return {
    sessionId: 'test-session',
    workspacePath,
    get sourcesPath() { return sourcesPath; },
    get skillsPath() { return join(workspacePath, 'skills'); },
    plansFolderPath: join(workspacePath, 'plans'),
    callbacks: {
      onPlanSubmitted: () => {},
      onAuthRequest: () => {},
    },
    fs: {
      exists: (path: string) => existsSync(path),
      readFile: (path: string) => readFileSync(path, 'utf-8'),
      readFileBuffer: (path: string) => readFileSync(path),
      writeFile: (path: string, content: string) => {
        const dir = path.slice(0, path.lastIndexOf('/'));
        mkdirSync(dir, { recursive: true });
        writeFileSync(path, content, 'utf-8');
      },
      isDirectory: (path: string) => existsSync(path) && statSync(path).isDirectory(),
      readdir: (path: string) => readdirSync(path),
      stat: (path: string) => {
        const stats = statSync(path);
        return { size: stats.size, isDirectory: () => stats.isDirectory() };
      },
    },
    loadSourceConfig: () => null,
  };
}

describe('handleSourceCreate', () => {
  let workspacePath: string;
  let ctx: SessionToolContext;

  beforeEach(() => {
    workspacePath = mkdtempSync(join(tmpdir(), 'source-create-test-'));
    ctx = createTestContext(workspacePath);
  });

  afterEach(() => {
    rmSync(workspacePath, { recursive: true, force: true });
  });

  it('creates MCP source config and guide', async () => {
    const result = await handleSourceCreate(ctx, {
      name: 'Linear',
      type: 'mcp',
      provider: 'linear',
      mcp: {
        transport: 'http',
        url: 'https://mcp.linear.app',
        authType: 'oauth',
      },
    });

    expect(result.isError).toBe(false);
    const sourceDir = join(workspacePath, 'sources', 'linear');
    const configPath = join(sourceDir, 'config.json');
    const guidePath = join(sourceDir, 'guide.md');

    expect(existsSync(configPath)).toBe(true);
    expect(existsSync(guidePath)).toBe(true);

    const config = JSON.parse(readFileSync(configPath, 'utf-8')) as { slug: string; type: string; provider: string; mcp?: { url?: string } };
    expect(config.slug).toBe('linear');
    expect(config.type).toBe('mcp');
    expect(config.provider).toBe('linear');
    expect(config.mcp?.url).toBe('https://mcp.linear.app');
  });

  it('returns error when MCP source is missing url/command', async () => {
    const result = await handleSourceCreate(ctx, {
      name: 'Broken MCP',
      type: 'mcp',
      provider: 'custom',
      mcp: {
        transport: 'http',
      },
    });

    expect(result.isError).toBe(true);
    expect(result.content[0]?.text).toContain('mcp.url');
  });

  it('creates unique slug when preferred slug exists', async () => {
    await handleSourceCreate(ctx, {
      name: 'Linear',
      type: 'mcp',
      provider: 'linear',
      mcp: {
        transport: 'http',
        url: 'https://mcp.linear.app',
        authType: 'oauth',
      },
    });

    const second = await handleSourceCreate(ctx, {
      name: 'Linear',
      type: 'mcp',
      provider: 'linear',
      mcp: {
        transport: 'http',
        url: 'https://mcp.linear.app',
        authType: 'oauth',
      },
    });

    expect(second.isError).toBe(false);
    expect(existsSync(join(workspacePath, 'sources', 'linear-2', 'config.json'))).toBe(true);
  });
});
