import { describe, it, expect } from 'bun:test'
import { renderMustache } from '../mustache'

describe('renderMustache', () => {
  // ============================================================
  // Variable Interpolation
  // ============================================================

  describe('variable interpolation', () => {
    it('replaces simple variables', () => {
      expect(renderMustache('Hello {{name}}!', { name: 'World' }))
        .toBe('Hello World!')
    })

    it('renders multiple variables', () => {
      expect(renderMustache('{{first}} {{last}}', { first: 'Jane', last: 'Doe' }))
        .toBe('Jane Doe')
    })

    it('renders missing variables as empty string', () => {
      expect(renderMustache('Hello {{name}}!', {}))
        .toBe('Hello !')
    })

    it('renders null/undefined as empty string', () => {
      expect(renderMustache('{{a}}|{{b}}', { a: null, b: undefined }))
        .toBe('|')
    })

    it('renders numbers and booleans as strings', () => {
      expect(renderMustache('{{count}} {{active}}', { count: 42, active: true }))
        .toBe('42 true')
    })

    it('trims whitespace in tag names', () => {
      expect(renderMustache('{{ name }}', { name: 'test' }))
        .toBe('test')
    })
  })

  // ============================================================
  // HTML Escaping
  // ============================================================

  describe('HTML escaping', () => {
    it('escapes HTML in double-mustache variables', () => {
      expect(renderMustache('{{text}}', { text: '<script>alert("xss")</script>' }))
        .toBe('&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;')
    })

    it('escapes ampersands and quotes', () => {
      expect(renderMustache('{{text}}', { text: "A & B's \"value\"" }))
        .toBe('A &amp; B&#39;s &quot;value&quot;')
    })

    it('does NOT escape triple-mustache variables', () => {
      expect(renderMustache('{{{html}}}', { html: '<b>bold</b>' }))
        .toBe('<b>bold</b>')
    })
  })

  // ============================================================
  // Sections (Conditionals)
  // ============================================================

  describe('sections (conditionals)', () => {
    it('renders section when value is truthy', () => {
      expect(renderMustache('{{#show}}visible{{/show}}', { show: true }))
        .toBe('visible')
    })

    it('skips section when value is falsy', () => {
      expect(renderMustache('{{#show}}visible{{/show}}', { show: false }))
        .toBe('')
    })

    it('skips section when value is null', () => {
      expect(renderMustache('{{#show}}visible{{/show}}', { show: null }))
        .toBe('')
    })

    it('skips section when value is missing', () => {
      expect(renderMustache('{{#show}}visible{{/show}}', {}))
        .toBe('')
    })

    it('skips section when value is empty string', () => {
      expect(renderMustache('{{#show}}visible{{/show}}', { show: '' }))
        .toBe('')
    })

    it('renders section with object context', () => {
      expect(renderMustache('{{#user}}{{name}}{{/user}}', { user: { name: 'Alice' } }))
        .toBe('Alice')
    })
  })

  // ============================================================
  // Sections (Loops)
  // ============================================================

  describe('sections (loops)', () => {
    it('iterates over arrays', () => {
      const data = { items: [{ name: 'A' }, { name: 'B' }, { name: 'C' }] }
      expect(renderMustache('{{#items}}{{name}} {{/items}}', data))
        .toBe('A B C ')
    })

    it('skips empty arrays', () => {
      expect(renderMustache('{{#items}}{{name}}{{/items}}', { items: [] }))
        .toBe('')
    })

    it('handles arrays of primitives with dot notation', () => {
      expect(renderMustache('{{#tags}}{{.}} {{/tags}}', { tags: ['a', 'b', 'c'] }))
        .toBe('a b c ')
    })
  })

  // ============================================================
  // Inverted Sections
  // ============================================================

  describe('inverted sections', () => {
    it('renders when value is falsy', () => {
      expect(renderMustache('{{^show}}hidden{{/show}}', { show: false }))
        .toBe('hidden')
    })

    it('renders when value is missing', () => {
      expect(renderMustache('{{^items}}no items{{/items}}', {}))
        .toBe('no items')
    })

    it('renders when array is empty', () => {
      expect(renderMustache('{{^items}}no items{{/items}}', { items: [] }))
        .toBe('no items')
    })

    it('skips when value is truthy', () => {
      expect(renderMustache('{{^show}}hidden{{/show}}', { show: true }))
        .toBe('')
    })

    it('skips when array has items', () => {
      expect(renderMustache('{{^items}}no items{{/items}}', { items: [1] }))
        .toBe('')
    })
  })

  // ============================================================
  // Nested Context & Dot Paths
  // ============================================================

  describe('nested context', () => {
    it('resolves dot-path variables', () => {
      expect(renderMustache('{{user.name}}', { user: { name: 'Alice' } }))
        .toBe('Alice')
    })

    it('resolves deep dot paths', () => {
      expect(renderMustache('{{a.b.c}}', { a: { b: { c: 'deep' } } }))
        .toBe('deep')
    })

    it('falls back to parent context in loops', () => {
      const data = { title: 'List', items: [{ name: 'A' }, { name: 'B' }] }
      expect(renderMustache('{{#items}}{{title}}: {{name}} {{/items}}', data))
        .toBe('List: A List: B ')
    })
  })

  // ============================================================
  // Comments
  // ============================================================

  describe('comments', () => {
    it('strips comments from output', () => {
      expect(renderMustache('before{{! this is a comment }}after', {}))
        .toBe('beforeafter')
    })
  })

  // ============================================================
  // Nested Sections
  // ============================================================

  describe('nested sections', () => {
    it('handles nested sections with different keys', () => {
      const data = { a: true, b: true }
      expect(renderMustache('{{#a}}{{#b}}both{{/b}}{{/a}}', data))
        .toBe('both')
    })

    it('handles nested sections with parent/child keys', () => {
      const data = { groups: [{ items: [{ x: 1 }, { x: 2 }] }, { items: [{ x: 3 }] }] }
      expect(renderMustache('{{#groups}}[{{#items}}{{x}}{{/items}}]{{/groups}}', data))
        .toBe('[12][3]')
    })
  })

  // ============================================================
  // Real-World Template Snippet
  // ============================================================

  describe('real-world template', () => {
    it('renders a Linear issue-like template', () => {
      const template = `<h1>{{identifier}}: {{title}}</h1>
<span>{{status}}</span>
{{#assignee}}<p>Assigned to: {{assignee}}</p>{{/assignee}}
{{^assignee}}<p>Unassigned</p>{{/assignee}}
{{#labels}}<div>{{#items}}<span>{{name}}</span> {{/items}}</div>{{/labels}}`

      const data = {
        identifier: 'ENG-42',
        title: 'Fix crash',
        status: 'In Progress',
        assignee: 'Jane',
        labels: { items: [{ name: 'bug' }, { name: 'P1' }] },
      }

      const result = renderMustache(template, data)
      expect(result).toContain('ENG-42: Fix crash')
      expect(result).toContain('Assigned to: Jane')
      expect(result).not.toContain('Unassigned')
      expect(result).toContain('<span>bug</span>')
      expect(result).toContain('<span>P1</span>')
    })

    it('renders template with missing optional data', () => {
      const template = `<h1>{{title}}</h1>
{{#description}}<p>{{description}}</p>{{/description}}
{{^description}}<p>No description</p>{{/description}}`

      const result = renderMustache(template, { title: 'Test' })
      expect(result).toContain('<h1>Test</h1>')
      expect(result).toContain('No description')
    })
  })
})
