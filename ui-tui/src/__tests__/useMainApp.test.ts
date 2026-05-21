import { describe, expect, it } from 'vitest'

import { findDrainableQueuedSubmissionIndex, shouldDrainQueuedSubmission } from '../app/useMainApp.js'

describe('useMainApp queue drain helper', () => {
  it('drains untargeted queued submissions in the visible session', () => {
    expect(shouldDrainQueuedSubmission({ text: 'hello' }, 'sid-b')).toBe(true)
  })

  it('drains queued submissions captured for the current visible session', () => {
    expect(shouldDrainQueuedSubmission({ sid: 'sid-a', text: 'run OMX' }, 'sid-a')).toBe(true)
  })

  it('does not drain queued submissions captured for a different session', () => {
    expect(shouldDrainQueuedSubmission({ sid: 'sid-a', text: 'run OMX' }, 'sid-b')).toBe(false)
  })

  it('finds the first later queued submission that matches the visible session', () => {
    expect(
      findDrainableQueuedSubmissionIndex(
        [
          { sid: 'sid-a', text: 'run for A' },
          { sid: 'sid-b', text: 'run for B' }
        ],
        'sid-b'
      )
    ).toBe(1)
  })

  it('reports no drainable queued submission when every captured entry belongs to another session', () => {
    expect(
      findDrainableQueuedSubmissionIndex(
        [
          { sid: 'sid-a', text: 'run for A' },
          { sid: 'sid-c', text: 'run for C' }
        ],
        'sid-b'
      )
    ).toBe(-1)
  })
})
