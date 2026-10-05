import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'

import { getHermesConfigLocks, type ProfileScope } from '@/hermes'
import { $activeConnectionId } from '@/store/connections'
import type { ConfigLock, ConfigLocksResponse } from '@/types/hermes'

import { hermesConfigKey } from './use-config-record'

const OPEN: ConfigLock = { locked: false, reason: null }

export const REAL_PROFILE_LOCK_KEY = 'browser.use_real_profile'

/** Lock state for one dotted config key, read from `GET /api/config/locks`.
 *
 *  Cached beside the config record for the same gateway/profile slot. A backend
 *  without the endpoint (older release) or a failed fetch counts as open: the
 *  backend still refuses a locked write, so the worst case there is today's
 *  behavior, never a silent write. `loaded` is false until the first answer
 *  (success or failure) so a consent prompt does not flash before we know. */
export function useConfigLock(
  key: string,
  profile?: ProfileScope
): { loaded: boolean; lock: ConfigLock } {
  const connectionId = useStore($activeConnectionId)

  const query = useQuery({
    queryKey: [...hermesConfigKey(profile, connectionId), 'locks'],
    queryFn: async (): Promise<ConfigLocksResponse> => {
      try {
        return await getHermesConfigLocks(profile ?? undefined)
      } catch {
        return {}
      }
    },
    staleTime: 60_000
  })

  return {
    loaded: query.data !== undefined,
    lock: query.data?.[key] ?? OPEN
  }
}
