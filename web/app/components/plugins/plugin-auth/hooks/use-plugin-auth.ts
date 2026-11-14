import type { PluginPayload } from '../types'
import { CredentialTypeEnum } from '../types'
import { useEffect, useRef } from 'react'
import {
  useGetPluginCredentialInfoHook,
  useInvalidPluginCredentialInfoHook,
} from './use-credential'

export const usePluginAuth = (
  pluginPayload: PluginPayload,
  enable?: boolean,
  includeCredentialIds?: string[],
) => {
  const { data, isLoading, isError } = useGetPluginCredentialInfoHook(
    pluginPayload,
    enable,
    includeCredentialIds,
  )
  // Keep the last loaded credential info while a refetch is in flight (e.g.
  // selecting a credential changes the query key). Without this, `data`
  // briefly becomes undefined, `isAuthorized` flips to false, and callers
  // that switch on it unmount their trigger — dropping keyboard focus to
  // `document.body`. A failed refetch still clears the retained data, so an
  // error surfaces as unauthorized, as before.
  const previousDataRef = useRef(data)
  useEffect(() => {
    if (data)
      previousDataRef.current = data
  }, [data])
  const effectiveData = data ?? (!isError ? previousDataRef.current : undefined)
  const isAuthorized = !!effectiveData?.credentials.length
  const canOAuth = effectiveData?.supported_credential_types.includes(CredentialTypeEnum.OAUTH2)
  const canApiKey = effectiveData?.supported_credential_types.includes(CredentialTypeEnum.API_KEY)
  const invalidPluginCredentialInfo = useInvalidPluginCredentialInfoHook(pluginPayload)

  return {
    isAuthorized,
    canOAuth,
    canApiKey,
    isLoading,
    credentials: effectiveData?.credentials || [],
    notAllowCustomCredential: effectiveData?.allow_custom_token === false,
    invalidPluginCredentialInfo,
  }
}
