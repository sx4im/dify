import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { AuthCategory, CredentialTypeEnum } from '../../types'
import { usePluginAuth } from '../use-plugin-auth'

// Mock dependencies
const mockCredentials = [
  { id: '1', credential_type: CredentialTypeEnum.API_KEY, is_default: false },
  { id: '2', credential_type: CredentialTypeEnum.OAUTH2, is_default: true },
]

const mockCredentialInfo = vi.fn().mockReturnValue({
  credentials: mockCredentials,
  supported_credential_types: [CredentialTypeEnum.API_KEY, CredentialTypeEnum.OAUTH2],
  allow_custom_token: true,
})

const mockInvalidate = vi.fn()

const mockIsError = vi.fn().mockReturnValue(false)

vi.mock('../use-credential', () => ({
  useGetPluginCredentialInfoHook: (_payload: unknown, enable?: boolean) => ({
    data: enable ? mockCredentialInfo() : undefined,
    isLoading: false,
    isError: mockIsError(),
  }),
  useInvalidPluginCredentialInfoHook: () => mockInvalidate,
}))

const basePayload = {
  category: AuthCategory.tool,
  provider: 'test-provider',
}

describe('usePluginAuth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('should return authorized state when credentials exist', () => {
    const { result } = renderHook(() => usePluginAuth(basePayload, true))

    expect(result.current.isAuthorized).toBe(true)
    expect(result.current.credentials).toEqual(mockCredentials)
    expect(result.current.canOAuth).toBe(true)
    expect(result.current.canApiKey).toBe(true)
    expect(result.current.notAllowCustomCredential).toBe(false)
  })

  it('should return unauthorized when enable is false', () => {
    const { result } = renderHook(() => usePluginAuth(basePayload, false))

    expect(result.current.isAuthorized).toBe(false)
    expect(result.current.credentials).toEqual([])
  })

  it('should handle empty credentials', () => {
    mockCredentialInfo.mockReturnValueOnce({
      credentials: [],
      supported_credential_types: [],
      allow_custom_token: false,
    })

    const { result } = renderHook(() => usePluginAuth(basePayload, true))

    expect(result.current.isAuthorized).toBe(false)
    expect(result.current.canOAuth).toBe(false)
    expect(result.current.canApiKey).toBe(false)
    expect(result.current.notAllowCustomCredential).toBe(true)
  })

  it('should handle only API Key support', () => {
    mockCredentialInfo.mockReturnValueOnce({
      credentials: [{ id: '1' }],
      supported_credential_types: [CredentialTypeEnum.API_KEY],
      allow_custom_token: true,
    })

    const { result } = renderHook(() => usePluginAuth(basePayload, true))

    expect(result.current.canApiKey).toBe(true)
    expect(result.current.canOAuth).toBe(false)
  })

  it('should keep previous credentials while credential info refetches', () => {
    const { result, rerender } = renderHook(() => usePluginAuth(basePayload, true))

    expect(result.current.isAuthorized).toBe(true)
    expect(result.current.credentials).toEqual(mockCredentials)

    // Selecting a credential changes the credential-info query key, so the
    // new query briefly has no data while it refetches. The previously
    // loaded info must be retained so callers do not unmount their trigger
    // and drop keyboard focus (langgenius/dify#46384).
    mockCredentialInfo.mockReturnValue(undefined as never)
    rerender()

    expect(result.current.isAuthorized).toBe(true)
    expect(result.current.credentials).toEqual(mockCredentials)

    // clearAllMocks does not reset implementations, so restore the default.
    mockCredentialInfo.mockReturnValue({
      credentials: mockCredentials,
      supported_credential_types: [CredentialTypeEnum.API_KEY, CredentialTypeEnum.OAUTH2],
      allow_custom_token: true,
    })
  })

  it('should drop retained credentials when the refetch errors', () => {
    const { result, rerender } = renderHook(() => usePluginAuth(basePayload, true))

    expect(result.current.isAuthorized).toBe(true)

    mockCredentialInfo.mockReturnValue(undefined as never)
    mockIsError.mockReturnValue(true)
    rerender()

    expect(result.current.isAuthorized).toBe(false)
    expect(result.current.credentials).toEqual([])

    // clearAllMocks does not reset implementations, so restore the defaults.
    mockCredentialInfo.mockReturnValue({
      credentials: mockCredentials,
      supported_credential_types: [CredentialTypeEnum.API_KEY, CredentialTypeEnum.OAUTH2],
      allow_custom_token: true,
    })
    mockIsError.mockReturnValue(false)
  })
})
