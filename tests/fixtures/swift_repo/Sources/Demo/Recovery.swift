import Foundation

enum PlatformMode {
    case common

    #if os(Windows)
    case windows
    #elseif os(Linux)
    case linux
    #else
    case other
    #endif
}

#if canImport(Foundation)
func foundationOnly() {}
#else
func fallbackOnly() {}
#endif

@available(
    *,
    deprecated,
    message: """
        Use `foundationOnly()` instead of \
        `fallbackOnly()` when available.
        """
)
func attributedRecovery() {}
