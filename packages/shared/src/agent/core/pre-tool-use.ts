/**
 * Shared PreToolUse utilities
 *
 * These functions are used by both ClaudeAgent and CodexAgent to ensure
 * consistent behavior across backends. Each function handles a specific
 * aspect of the PreToolUse hook:
 *
 * 1. Path expansion: Expand ~ to home directory in file paths
 * 2. Skill qualification: Add workspace prefix to skill names
 * 3. MCP metadata stripping: Remove UI-only fields from tool inputs
 * 4. Config validation: Validate workspace config files before writes
 */

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expandPath } from '../../utils/paths.ts';
import {
  detectConfigFileType,
  detectAppConfigFileType,
  validateConfigFileContent,
  formatValidationResult,
  type ConfigFileDetection,
} from '../../config/validators.ts';
import { AGENTS_PLUGIN_NAME } from '../../skills/types.ts';
import { GLOBAL_AGENT_SKILLS_DIR, PROJECT_AGENT_SKILLS_DIR } from '../../skills/storage.ts';

// ============================================================
// TYPES
// ============================================================

export interface PreToolUseContext {
  /** Current working directory or workspace root */
  workspaceRootPath: string;
  /** Workspace ID for skill qualification */
  workspaceId: string;
  /** Debug callback */
  onDebug?: (message: string) => void;
}

export interface PathExpansionResult {
  /** Whether any paths were modified */
  modified: boolean;
  /** The updated input (or original if not modified) */
  input: Record<string, unknown>;
}

export interface SkillQualificationResult {
  /** Whether the skill name was qualified */
  modified: boolean;
  /** The updated input */
  input: Record<string, unknown>;
}

export interface MetadataStrippingResult {
  /** Whether metadata was stripped */
  modified: boolean;
  /** The cleaned input */
  input: Record<string, unknown>;
}

export interface ConfigValidationResult {
  /** Whether validation passed */
  valid: boolean;
  /** Error message if validation failed */
  error?: string;
}

// ============================================================
// BUILT-IN TOOLS
// ============================================================

/** SDK built-in tools that should NOT have metadata stripped */
export const BUILT_IN_TOOLS = new Set([
  'Bash',
  'Read',
  'Write',
  'Edit',
  'Glob',
  'Grep',
  'WebFetch',
  'WebSearch',
  'Task',
  'TaskOutput',
  'TodoWrite',
  'MultiEdit',
  'NotebookEdit',
  'KillShell',
  'SubmitPlan',
  'Skill',
  'SlashCommand',
  'TaskStop',
]);

/** Tools that operate on file paths */
export const FILE_PATH_TOOLS = new Set([
  'Read',
  'Write',
  'Edit',
  'MultiEdit',
  'Glob',
  'Grep',
  'NotebookEdit',
]);

/** Tools that can write config files */
export const CONFIG_WRITE_TOOLS = new Set(['Write', 'Edit']);

// ============================================================
// PATH EXPANSION
// ============================================================

/**
 * Expand ~ paths in file tool inputs.
 *
 * Handles multiple path parameters:
 * - file_path: Used by Read, Write, Edit, MultiEdit
 * - notebook_path: Used by NotebookEdit
 * - path: Used by Glob, Grep
 *
 * @param toolName - The SDK tool name
 * @param input - The tool input object
 * @param onDebug - Optional debug callback
 * @returns PathExpansionResult with modified flag and updated input
 */
export function expandToolPaths(
  toolName: string,
  input: Record<string, unknown>,
  onDebug?: (message: string) => void
): PathExpansionResult {
  if (!FILE_PATH_TOOLS.has(toolName)) {
    return { modified: false, input };
  }

  let updatedInput: Record<string, unknown> | null = null;

  // Expand file_path if present and starts with ~
  if (typeof input.file_path === 'string' && input.file_path.startsWith('~')) {
    const expandedPath = expandPath(input.file_path);
    onDebug?.(`Expanding path: ${input.file_path} → ${expandedPath}`);
    updatedInput = { ...input, file_path: expandedPath };
  }

  // Expand notebook_path if present and starts with ~
  if (typeof input.notebook_path === 'string' && input.notebook_path.startsWith('~')) {
    const expandedPath = expandPath(input.notebook_path);
    onDebug?.(`Expanding notebook path: ${input.notebook_path} → ${expandedPath}`);
    updatedInput = { ...(updatedInput || input), notebook_path: expandedPath };
  }

  // Expand path if present and starts with ~ (for Glob, Grep)
  if (typeof input.path === 'string' && input.path.startsWith('~')) {
    const expandedPath = expandPath(input.path);
    onDebug?.(`Expanding search path: ${input.path} → ${expandedPath}`);
    updatedInput = { ...(updatedInput || input), path: expandedPath };
  }

  return {
    modified: updatedInput !== null,
    input: updatedInput || input,
  };
}

// ============================================================
// SKILL QUALIFICATION
// ============================================================

/**
 * Ensure skill names are fully-qualified with the correct plugin prefix.
 *
 * The SDK resolves skills as `pluginName:skillSlug` where the plugin name is
 * read from `.claude-plugin/plugin.json` `name` field. Skills can live in 3 tiers:
 *   1. Workspace: {workspaceRoot}/skills/{slug}/ → plugin name from plugin.json
 *   2. Project:   {workingDir}/.agents/skills/{slug}/ → plugin name = ".agents"
 *   3. Global:    ~/.agents/skills/{slug}/ → plugin name = ".agents"
 *
 * This function resolves the bare slug to the correct plugin prefix by checking
 * which directory actually contains the skill. It also handles re-qualifying
 * skills that were incorrectly qualified by the UI (which always uses the
 * workspace slug, even for global/project skills).
 *
 * @param input - The Skill tool input ({ skill: string, args?: string })
 * @param workspaceSlug - The workspace slug (from .claude-plugin/plugin.json name)
 * @param workspaceRootPath - Absolute path to the workspace root
 * @param workingDirectory - Absolute path to the current working directory (optional)
 * @param onDebug - Optional debug callback
 * @returns SkillQualificationResult with modified flag and updated input
 */
export function qualifySkillName(
  input: Record<string, unknown>,
  workspaceSlug: string,
  workspaceRootPath?: string,
  workingDirectory?: string,
  onDebug?: (message: string) => void
): SkillQualificationResult {
  const skill = input.skill as string | undefined;
  if (!skill) return { modified: false, input };

  // Extract the bare slug — strip any existing qualifier (e.g. "CraftAgentWS:commit" → "commit")
  const bareSlug = skill.includes(':') ? skill.split(':').pop()! : skill;
  if (!bareSlug) return { modified: false, input };

  // If we don't have the workspace root path, fall back to simple workspace-only qualification
  if (!workspaceRootPath) {
    if (skill.includes(':')) return { modified: false, input };
    const qualifiedSkill = `${workspaceSlug}:${skill}`;
    onDebug?.(`Skill tool: qualified "${skill}" → "${qualifiedSkill}" (legacy fallback)`);
    return { modified: true, input: { ...input, skill: qualifiedSkill } };
  }

  // Resolve which plugin tier contains this skill by checking SKILL.md existence
  const resolvedSkill = resolveSkillPlugin(bareSlug, workspaceSlug, workspaceRootPath, workingDirectory);

  if (resolvedSkill === skill) {
    // Already correctly qualified
    return { modified: false, input };
  }

  onDebug?.(`Skill tool: qualified "${skill}" → "${resolvedSkill}"`);
  return {
    modified: true,
    input: { ...input, skill: resolvedSkill },
  };
}

/**
 * Resolve a skill slug to its fully-qualified plugin:slug name by checking
 * which plugin directory actually contains the skill.
 */
function resolveSkillPlugin(
  bareSlug: string,
  workspaceSlug: string,
  workspaceRootPath: string,
  workingDirectory?: string,
): string {
  // Priority order matches loadAllSkills: project (highest) > workspace > global (lowest)

  // 1. Project: {workingDir}/.agents/skills/{slug}/SKILL.md
  if (workingDirectory && existsSync(join(workingDirectory, PROJECT_AGENT_SKILLS_DIR, bareSlug, 'SKILL.md'))) {
    return `${AGENTS_PLUGIN_NAME}:${bareSlug}`;
  }

  // 2. Workspace: {workspaceRoot}/skills/{slug}/SKILL.md
  if (existsSync(join(workspaceRootPath, 'skills', bareSlug, 'SKILL.md'))) {
    return `${workspaceSlug}:${bareSlug}`;
  }

  // 3. Global: ~/.agents/skills/{slug}/SKILL.md
  if (existsSync(join(GLOBAL_AGENT_SKILLS_DIR, bareSlug, 'SKILL.md'))) {
    return `${AGENTS_PLUGIN_NAME}:${bareSlug}`;
  }

  // Fallback: assume workspace plugin (original behavior)
  return `${workspaceSlug}:${bareSlug}`;
}

// ============================================================
// MCP METADATA STRIPPING
// ============================================================

/**
 * Strip _intent and _displayName metadata from tool inputs.
 *
 * These fields are injected into all tool schemas by the network interceptor
 * so Claude provides semantic intent for UI display. They must be stripped
 * before execution to avoid SDK validation errors and MCP server rejections.
 *
 * The extraction for UI happens in tool-matching.ts BEFORE this stripping.
 *
 * @param toolName - The tool name
 * @param input - The tool input object
 * @param onDebug - Optional debug callback
 * @returns MetadataStrippingResult with modified flag and cleaned input
 */
export function stripToolMetadata(
  toolName: string,
  input: Record<string, unknown>,
  onDebug?: (message: string) => void
): MetadataStrippingResult {
  const hasMetadata = '_intent' in input || '_displayName' in input;

  if (!hasMetadata) {
    return { modified: false, input };
  }

  // Strip the metadata fields
  const { _intent, _displayName, ...cleanInput } = input;
  onDebug?.(`Stripped tool metadata from ${toolName}: _intent=${!!_intent}, _displayName=${!!_displayName}`);

  return {
    modified: true,
    input: cleanInput,
  };
}

/**
 * @deprecated Use stripToolMetadata instead. This alias is kept for backwards compatibility.
 */
export const stripMcpMetadata = stripToolMetadata;

// ============================================================
// CONFIG FILE VALIDATION
// ============================================================

/**
 * Validate config file writes before they happen.
 *
 * For Write/Edit operations on workspace config files, validates the
 * resulting content before allowing the write to proceed. This prevents
 * invalid configs from ever reaching disk.
 *
 * Validates:
 * - sources/{slug}/config.json
 * - skills/{slug}/SKILL.md
 * - statuses/config.json
 * - permissions.json
 * - theme.json
 * - tool-icons/tool-icons.json
 *
 * @param toolName - 'Write' or 'Edit'
 * @param input - The tool input (with expanded paths)
 * @param workspaceRootPath - The workspace root path for detection
 * @param onDebug - Optional debug callback
 * @returns ConfigValidationResult with valid flag and optional error
 */
export function validateConfigWrite(
  toolName: string,
  input: Record<string, unknown>,
  workspaceRootPath: string,
  onDebug?: (message: string) => void
): ConfigValidationResult {
  if (!CONFIG_WRITE_TOOLS.has(toolName)) {
    return { valid: true };
  }

  const filePath = input.file_path as string | undefined;
  if (!filePath) {
    return { valid: true };
  }

  // Check workspace-scoped configs first, then app-level configs
  const detection: ConfigFileDetection | null =
    detectConfigFileType(filePath, workspaceRootPath) ?? detectAppConfigFileType(filePath);

  if (!detection) {
    // Not a config file - allow
    return { valid: true };
  }

  let contentToValidate: string | null = null;

  if (toolName === 'Write') {
    // For Write, the full file content is in input.content
    contentToValidate = input.content as string;
  } else if (toolName === 'Edit') {
    // For Edit, simulate the replacement on the current file content
    try {
      const currentContent = readFileSync(filePath, 'utf-8');
      const oldString = input.old_string as string;
      const newString = input.new_string as string;
      const replaceAll = input.replace_all as boolean | undefined;
      contentToValidate = replaceAll
        ? currentContent.replaceAll(oldString, newString)
        : currentContent.replace(oldString, newString);
    } catch {
      // File doesn't exist yet or can't be read — skip validation
      // (Write tool will create it; Edit will fail on its own)
      return { valid: true };
    }
  }

  if (!contentToValidate) {
    return { valid: true };
  }

  const validationResult = validateConfigFileContent(detection, contentToValidate);

  if (validationResult && !validationResult.valid) {
    onDebug?.(
      `Config validation blocked ${toolName} to ${detection.displayFile}: ${validationResult.errors.length} errors`
    );
    return {
      valid: false,
      error: `Cannot write invalid config to ${detection.displayFile}.\n\n${formatValidationResult(validationResult)}\n\nFix the errors above and try again.`,
    };
  }

  return { valid: true };
}
