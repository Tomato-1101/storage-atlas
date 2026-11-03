// iCloud Drive のファイルのアップロード状態を 1 引数 1 行の JSON で出す（stor offload が使う。読むだけ）。
// 出力（JSON Lines）: {"error":エラー,"local":Current|Downloaded|NotDownloaded|-,"path":パス,"state":uploaded|uploading|pending}
// パスを JSON 文字列で出すのは、ファイル名の改行・タブで別のファイルの行を偽装させないため。
// ビルド: swiftc -O tools/icstat.swift -o ~/.local/share/stor/bin/icstat（stor offload が無ければ自動でビルドする）
import Foundation
for p in CommandLine.arguments.dropFirst() {
  let u = URL(fileURLWithPath: p)
  let v = try? u.resourceValues(forKeys: [.ubiquitousItemIsUploadedKey, .ubiquitousItemIsUploadingKey, .ubiquitousItemDownloadingStatusKey, .ubiquitousItemUploadingErrorKey])
  let state = v?.ubiquitousItemIsUploaded == true ? "uploaded" : (v?.ubiquitousItemIsUploading == true ? "uploading" : "pending")
  let local = v?.ubiquitousItemDownloadingStatus?.rawValue.replacingOccurrences(of: "NSURLUbiquitousItemDownloadingStatus", with: "") ?? "-"
  let err = v?.ubiquitousItemUploadingError?.localizedDescription ?? ""
  let row: [String: String] = ["path": p, "state": state, "local": local, "error": err]
  let data = try! JSONSerialization.data(withJSONObject: row, options: [.sortedKeys])
  print(String(decoding: data, as: UTF8.self))
}
