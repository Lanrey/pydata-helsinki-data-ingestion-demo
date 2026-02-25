/**
 * Source Create Handler
 *
 * Creates a source folder with config.json and a starter guide.md.
 * This provides a deterministic alternative to ad-hoc file creation.
 */

import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { SessionToolContext } from '../context.ts';
import type { ToolResult, SourceConfig, SourceType, McpSourceConfig, ApiSourceConfig, LocalSourceConfig } from '../types.ts';
import { successResponse, errorResponse } from '../response.ts';
import { sourceExists, listSourceSlugs } from '../source-helpers.ts';

export interface SourceCreateArgs {
  name: string;
  type: SourceType;
  provider?: string;
  slug?: string;
  enabled?: boolean;
  tagline?: string;
  icon?: string;
  mcp?: McpSourceConfig;
  api?: ApiSourceConfig;
  local?: LocalSourceConfig;
}

function slugify(value: string): string {
  const slug = value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 50);
  return slug || 'source';
}

function resolveUniqueSlug(workspacePath: string, preferredSlug: string): string {
  const base = slugify(preferredSlug);
  const existing = new Set(listSourceSlugs(workspacePath));
  if (!existing.has(base)) return base;

  let counter = 2;
  while (existing.has(`${base}-${counter}`)) counter++;
  return `${base}-${counter}`;
}

function validateTypeSpecificConfig(args: SourceCreateArgs): string | null {
  if (args.type === 'mcp') {
    if (!args.mcp) return 'MCP source requires an mcp configuration object.';
    const transport = args.mcp.transport ?? 'http';
    if (transport === 'stdio' && !args.mcp.command) {
      return 'MCP stdio source requires mcp.command.';
    }
    if ((transport === 'http' || transport === 'sse') && !args.mcp.url) {
      return 'MCP HTTP/SSE source requires mcp.url.';
    }
  }

  if (args.type === 'api') {
    if (!args.api) return 'API source requires an api configuration object.';
    if (!args.api.baseUrl) return 'API source requires api.baseUrl.';
    if (!args.api.authType) return 'API source requires api.authType.';
  }

  if (args.type === 'local') {
    if (!args.local) return 'Local source requires a local configuration object.';
    if (!args.local.path) return 'Local source requires local.path.';
  }

  return null;
}

function buildGuideTemplate(name: string): string {
  return `# ${name}

## Guidelines

(Add usage guidelines here)

## Context

(Add context about this source)
`;
}

/**
 * Handle source_create tool call.
 */
export async function handleSourceCreate(
  ctx: SessionToolContext,
  args: SourceCreateArgs
): Promise<ToolResult> {
  if (!args.name?.trim()) {
    return errorResponse('Source name is required.');
  }

  const typeError = validateTypeSpecificConfig(args);
  if (typeError) {
    return errorResponse(typeError);
  }

  const targetSlug = resolveUniqueSlug(ctx.workspacePath, args.slug || args.name);
  if (sourceExists(ctx.workspacePath, targetSlug)) {
    return errorResponse(`Source '${targetSlug}' already exists.`);
  }

  const sourceDir = join(ctx.sourcesPath, targetSlug);
  const configPath = join(sourceDir, 'config.json');
  const guidePath = join(sourceDir, 'guide.md');

  if (!existsSync(sourceDir)) {
    mkdirSync(sourceDir, { recursive: true });
  }

  const now = Date.now();
  const config: SourceConfig = {
    id: `${targetSlug}_${Math.random().toString(36).slice(2, 10)}`,
    name: args.name,
    slug: targetSlug,
    enabled: args.enabled ?? true,
    provider: args.provider || 'custom',
    type: args.type,
    ...(args.tagline ? { tagline: args.tagline } : {}),
    ...(args.icon ? { icon: args.icon } : {}),
    ...(args.type === 'mcp' ? { mcp: args.mcp } : {}),
    ...(args.type === 'api' ? { api: args.api } : {}),
    ...(args.type === 'local' ? { local: args.local } : {}),
    createdAt: now,
    updatedAt: now,
  };

  ctx.fs.writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`);
  if (!ctx.fs.exists(guidePath)) {
    ctx.fs.writeFile(guidePath, buildGuideTemplate(args.name));
  }

  return successResponse(
    `Created source '${targetSlug}' (${args.type}).\n\n` +
    `Files created:\n` +
    `- ${configPath}\n` +
    `- ${guidePath}\n\n` +
    `Next: run source_test with sourceSlug='${targetSlug}'.`
  );
}
