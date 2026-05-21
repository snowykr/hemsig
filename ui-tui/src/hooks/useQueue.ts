import { useCallback, useRef, useState } from 'react'

import type { QueuedSubmission } from '../app/interfaces.js'

// Mutates `arr` in place; returned reference is the same input array, kept
// so callers can chain. Use `Array.prototype.toSpliced` if you need a copy.
export function removeAtInPlace<T>(arr: T[], i: number): T[] {
  if (i < 0 || i >= arr.length) {
    return arr
  }

  arr.splice(i, 1)

  return arr
}

export function normalizeQueuedSubmission(entry: QueuedSubmission | string): QueuedSubmission {
  return typeof entry === 'string' ? { text: entry } : entry
}

export const shouldDrainQueuedSubmission = (
  entry: QueuedSubmission | undefined,
  currentSid?: null | string
): boolean => !entry?.sid || entry.sid === currentSid

export const findDrainableQueuedSubmissionIndex = (
  entries: readonly QueuedSubmission[],
  currentSid?: null | string
): number => entries.findIndex(entry => shouldDrainQueuedSubmission(entry, currentSid))

export function takeDrainableQueuedSubmission(
  entries: QueuedSubmission[],
  currentSid?: null | string
): QueuedSubmission | undefined {
  const index = findDrainableQueuedSubmissionIndex(entries, currentSid)

  if (index < 0) {
    return undefined
  }

  return entries.splice(index, 1)[0]
}

export function useQueue() {
  const queueRef = useRef<QueuedSubmission[]>([])
  const [queuedDisplay, setQueuedDisplay] = useState<string[]>([])
  const queueEditRef = useRef<number | null>(null)
  const [queueEditIdx, setQueueEditIdx] = useState<number | null>(null)

  const syncQueue = useCallback(() => setQueuedDisplay(queueRef.current.map(entry => entry.text)), [])

  const setQueueEdit = useCallback((idx: number | null) => {
    queueEditRef.current = idx
    setQueueEditIdx(idx)
  }, [])

  const enqueue = useCallback(
    (entry: QueuedSubmission | string) => {
      queueRef.current.push(normalizeQueuedSubmission(entry))
      syncQueue()
    },
    [syncQueue]
  )

  const dequeue = useCallback(() => {
    const head = queueRef.current.shift()
    syncQueue()

    return head
  }, [syncQueue])

  const replaceQ = useCallback(
    (i: number, text: string) => {
      const existing = queueRef.current[i]

      if (!existing) {
        return
      }

      queueRef.current[i] = { ...existing, text }
      syncQueue()
    },
    [syncQueue]
  )

  const removeQ = useCallback(
    (i: number) => {
      const before = queueRef.current.length

      removeAtInPlace(queueRef.current, i)

      if (queueRef.current.length !== before) {
        syncQueue()
      }
    },
    [syncQueue]
  )

  return {
    dequeue,
    enqueue,
    queueEditIdx,
    queueEditRef,
    queueRef,
    queuedDisplay,
    removeQ,
    replaceQ,
    setQueueEdit,
    syncQueue
  }
}
