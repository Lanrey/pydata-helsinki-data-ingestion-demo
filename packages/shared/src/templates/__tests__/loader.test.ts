import { describe, it, expect } from 'bun:test'
import { parseTemplateHeader, validateTemplateData, type TemplateMeta } from '../loader'

describe('parseTemplateHeader', () => {
  it('parses a full header with all tags', () => {
    const content = `<!--
  @template issue-detail
  @name Issue Detail
  @description Renders a single issue
  @required identifier, title, status
  @optional priority, assignee, team
-->
<!DOCTYPE html>`

    const meta = parseTemplateHeader(content)
    expect(meta).not.toBeNull()
    expect(meta!.id).toBe('issue-detail')
    expect(meta!.name).toBe('Issue Detail')
    expect(meta!.description).toBe('Renders a single issue')
    expect(meta!.required).toEqual(['identifier', 'title', 'status'])
    expect(meta!.optional).toEqual(['priority', 'assignee', 'team'])
  })

  it('returns null when no comment header exists', () => {
    expect(parseTemplateHeader('<!DOCTYPE html><html></html>')).toBeNull()
  })

  it('returns null when comment lacks @template tag', () => {
    const content = `<!-- This is just a regular comment -->
<!DOCTYPE html>`
    expect(parseTemplateHeader(content)).toBeNull()
  })

  it('handles minimal header (only @template)', () => {
    const content = `<!--
  @template my-template
-->
<html></html>`

    const meta = parseTemplateHeader(content)
    expect(meta).not.toBeNull()
    expect(meta!.id).toBe('my-template')
    expect(meta!.name).toBe('my-template') // Falls back to id
    expect(meta!.description).toBe('')
    expect(meta!.required).toEqual([])
    expect(meta!.optional).toEqual([])
  })

  it('handles leading whitespace before comment', () => {
    const content = `  \n  <!--
  @template test
  @name Test Template
-->
<html></html>`

    const meta = parseTemplateHeader(content)
    expect(meta).not.toBeNull()
    expect(meta!.id).toBe('test')
  })

  it('trims whitespace from tag values', () => {
    const content = `<!--
  @template   spaced-out
  @name   My Template
  @required   a , b , c
-->
<html></html>`

    const meta = parseTemplateHeader(content)
    expect(meta).not.toBeNull()
    expect(meta!.id).toBe('spaced-out')
    expect(meta!.name).toBe('My Template')
    expect(meta!.required).toEqual(['a', 'b', 'c'])
  })

  it('handles empty @required list', () => {
    const content = `<!--
  @template test
  @required
-->
<html></html>`

    const meta = parseTemplateHeader(content)
    expect(meta).not.toBeNull()
    // @required with no value after it won't match the regex (needs \s+ before content)
    expect(meta!.required).toEqual([])
  })
})

describe('validateTemplateData', () => {
  const meta: TemplateMeta = {
    id: 'test',
    name: 'Test',
    description: '',
    required: ['title', 'status', 'identifier'],
    optional: ['assignee', 'team'],
  }

  it('returns no warnings when all required fields present', () => {
    const warnings = validateTemplateData(meta, {
      title: 'Bug fix',
      status: 'Open',
      identifier: 'ENG-1',
    })
    expect(warnings).toEqual([])
  })

  it('returns warnings for missing required fields', () => {
    const warnings = validateTemplateData(meta, {
      title: 'Bug fix',
    })
    expect(warnings).toHaveLength(2)
    expect(warnings[0]!.field).toBe('status')
    expect(warnings[1]!.field).toBe('identifier')
  })

  it('returns warnings when required field is null', () => {
    const warnings = validateTemplateData(meta, {
      title: 'Bug fix',
      status: null,
      identifier: 'ENG-1',
    })
    expect(warnings).toHaveLength(1)
    expect(warnings[0]!.field).toBe('status')
  })

  it('does not warn about missing optional fields', () => {
    const warnings = validateTemplateData(meta, {
      title: 'Bug fix',
      status: 'Open',
      identifier: 'ENG-1',
      // assignee and team omitted — should not warn
    })
    expect(warnings).toEqual([])
  })

  it('returns no warnings when template has no required fields', () => {
    const noRequired: TemplateMeta = {
      id: 'test',
      name: 'Test',
      description: '',
      required: [],
      optional: ['a', 'b'],
    }
    const warnings = validateTemplateData(noRequired, {})
    expect(warnings).toEqual([])
  })

  it('allows falsy but present values (empty string, 0, false)', () => {
    const warnings = validateTemplateData(meta, {
      title: '',
      status: 0,
      identifier: false,
    })
    // empty string, 0, and false are present in the object (not null/undefined)
    expect(warnings).toEqual([])
  })
})
