import { describe, expect, it } from 'vitest'

import {
  findDrainableQueuedSubmissionIndex,
  normalizeQueuedSubmission,
  removeAtInPlace,
  shouldDrainQueuedSubmission,
  takeDrainableQueuedSubmission
} from '../hooks/useQueue.js'

describe('removeAtInPlace', () => {
  it('removes the item at the given index in place', () => {
    const arr = ['a', 'b', 'c']

    removeAtInPlace(arr, 1)
    expect(arr).toEqual(['a', 'c'])
  })

  it('is a no-op when the index is out of bounds', () => {
    const arr = ['a', 'b']

    removeAtInPlace(arr, -1)
    removeAtInPlace(arr, 5)
    expect(arr).toEqual(['a', 'b'])
  })

  it('returns the same reference (mutates in place)', () => {
    const arr = ['x']
    const same = removeAtInPlace(arr, 0)

    expect(same).toBe(arr)
    expect(arr).toEqual([])
  })
})

describe('normalizeQueuedSubmission', () => {
  it('wraps legacy string queue entries', () => {
    expect(normalizeQueuedSubmission('hello')).toEqual({ text: 'hello' })
  })

  it('preserves queued metadata objects', () => {
    const queued = { sid: 'sid-a', text: 'run OMX', workflowActivation: { workflow_id: 'omx_delegation' } }

    expect(normalizeQueuedSubmission(queued)).toBe(queued)
  })
})

describe('session-aware queue draining helpers', () => {
  it('treats untargeted queue entries as drainable in any session', () => {
    expect(shouldDrainQueuedSubmission({ text: 'hello' }, 'sid-b')).toBe(true)
  })

  it('finds the first queue entry captured for the visible session', () => {
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

  it('removes a later matching entry without dropping the foreign-session head', () => {
    const queue = [
      { sid: 'sid-a', text: 'run for A' },
      { sid: 'sid-b', text: 'run for B' }
    ]

    expect(takeDrainableQueuedSubmission(queue, 'sid-b')).toEqual({ sid: 'sid-b', text: 'run for B' })
    expect(queue).toEqual([{ sid: 'sid-a', text: 'run for A' }])
  })

  it('leaves the queue untouched when nothing belongs to the visible session', () => {
    const queue = [
      { sid: 'sid-a', text: 'run for A' },
      { sid: 'sid-c', text: 'run for C' }
    ]

    expect(takeDrainableQueuedSubmission(queue, 'sid-b')).toBeUndefined()
    expect(queue).toEqual([
      { sid: 'sid-a', text: 'run for A' },
      { sid: 'sid-c', text: 'run for C' }
    ])
  })
})
