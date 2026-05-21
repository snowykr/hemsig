import { describe, expect, it } from 'vitest'

import {
  canSteerSubmission,
  prepareEditedQueuedSubmission,
  queueSubmissionForLater,
  resolveBusyInputDisposition,
  resolveSubmissionSid,
  shouldCancelStaleSubmission
} from '../app/useSubmission.js'

describe('useSubmission helpers', () => {
  it('keeps workflow activation and originating session when queueing a targeted skill', () => {
    const activation = { workflow_id: 'omx_delegation' }

    expect(queueSubmissionForLater('run OMX', 'sid-a', activation)).toEqual({
      sid: 'sid-a',
      text: 'run OMX',
      workflowActivation: activation
    })
  })

  it('prefers the captured session id over the current live session', () => {
    expect(resolveSubmissionSid('sid-a', 'sid-b')).toBe('sid-a')
    expect(resolveSubmissionSid(undefined, 'sid-b')).toBe('sid-b')
    expect(resolveSubmissionSid('', 'sid-b')).toBe('sid-b')
  })

  it('cancels stale submissions when the visible session changed', () => {
    expect(shouldCancelStaleSubmission('sid-a', 'sid-b')).toBe(true)
    expect(shouldCancelStaleSubmission('sid-a', 'sid-a')).toBe(false)
    expect(shouldCancelStaleSubmission(undefined, 'sid-b')).toBe(false)
  })

  it('refuses to steer targeted workflow submissions', () => {
    expect(canSteerSubmission('plain text')).toBe(true)
    expect(canSteerSubmission({ text: 'plain text' })).toBe(true)
    expect(canSteerSubmission({ text: 'run OMX', workflowActivation: { workflow_id: 'omx_delegation' } })).toBe(false)
  })

  it('queues workflow-targeted submissions in steer mode instead of interrupting', () => {
    expect(
      resolveBusyInputDisposition('steer', 'sid-a', {
        sid: 'sid-a',
        text: 'run OMX',
        workflowActivation: { workflow_id: 'omx_delegation' }
      })
    ).toBe('queue')
  })

  it('keeps plain text steer submissions on the steer path', () => {
    expect(resolveBusyInputDisposition('steer', 'sid-a', 'plain text')).toBe('steer')
    expect(resolveBusyInputDisposition('steer', 'sid-a', { text: 'plain text' })).toBe('steer')
  })

  it('preserves an edited queued submission when the visible session changed', () => {
    expect(
      prepareEditedQueuedSubmission(
        { sid: 'sid-a', text: 'old text' },
        'new text',
        'sid-b'
      )
    ).toEqual({
      kind: 'stale',
      entry: { sid: 'sid-a', text: 'new text' }
    })
  })

  it('allows an edited queued submission for the visible session to be sent', () => {
    expect(
      prepareEditedQueuedSubmission(
        { sid: 'sid-a', text: 'old text' },
        'new text',
        'sid-a'
      )
    ).toEqual({
      kind: 'ready',
      entry: { sid: 'sid-a', text: 'new text' }
    })
  })
})
