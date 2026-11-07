import Foundation
import LocalAuthentication

// Exit 0: the user authenticated (Touch ID, Apple Watch or the login password).
// Exit 1: cancelled or failed. Exit 2: no authentication available (for example over ssh).
// `--can-evaluate` only checks availability and never prompts.

final class Outcome: @unchecked Sendable {
    var succeeded = false
    var detail = "cancelled"
}

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

let arguments = Array(CommandLine.arguments.dropFirst())
let context = LAContext()
var availability: NSError?
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &availability) else {
    fail("authentication is not available here: \(availability?.localizedDescription ?? "unknown")", code: 2)
}
if arguments.contains("--can-evaluate") {
    exit(0)
}

let reason = arguments.first(where: { !$0.hasPrefix("--") }) ?? "unlock your SecurID tokens"
let outcome = Outcome()
let finished = DispatchSemaphore(value: 0)
context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, error in
    outcome.succeeded = success
    if let error = error { outcome.detail = error.localizedDescription }
    finished.signal()
}
finished.wait()
if outcome.succeeded {
    exit(0)
}
fail(outcome.detail, code: 1)
