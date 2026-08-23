import Foundation
import Darwin

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(
        Data("Usage: GoraniImageUpload <supervisor-script>\n".utf8)
    )
    exit(64)
}

let supervisorPath = CommandLine.arguments[1]

// 최초 설치 시 앱 이름으로 로컬 네트워크 권한을 요청해 재부팅 후 SMB 탐색을 허용한다.
if let smbHost = ProcessInfo.processInfo.environment["GORANI_SMB_HOST"] {
    let networkProbe = Process()
    networkProbe.executableURL = URL(fileURLWithPath: "/usr/bin/nc")
    networkProbe.arguments = ["-G", "2", "-z", smbHost, "445"]
    networkProbe.standardOutput = FileHandle.nullDevice
    networkProbe.standardError = FileHandle.nullDevice
    do {
        try networkProbe.run()
        networkProbe.waitUntilExit()
    } catch {
        FileHandle.standardError.write(
            Data("Unable to probe SMB host: \(error)\n".utf8)
        )
    }
}

let process = Process()
process.executableURL = URL(fileURLWithPath: "/bin/zsh")
process.arguments = [supervisorPath]
process.environment = ProcessInfo.processInfo.environment

// launchd의 종료 신호를 실제 관리 스크립트까지 전달해 자식 서비스를 남기지 않는다.
let forwardedSignals: [Int32] = [SIGTERM, SIGINT, SIGHUP]
let signalSources = forwardedSignals.map { signalNumber -> DispatchSourceSignal in
    signal(signalNumber, SIG_IGN)
    let source = DispatchSource.makeSignalSource(
        signal: signalNumber,
        queue: DispatchQueue.global(qos: .utility)
    )
    source.setEventHandler {
        if process.isRunning {
            process.terminate()
        }
    }
    source.resume()
    return source
}

do {
    try process.run()
    withExtendedLifetime(signalSources) {
        process.waitUntilExit()
    }
    exit(process.terminationStatus)
} catch {
    FileHandle.standardError.write(
        Data("Unable to start upload supervisor: \(error)\n".utf8)
    )
    exit(1)
}
