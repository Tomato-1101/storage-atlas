// iCloud Drive のファイルのアップロード状態を 1 引数 1 行で出す（stor offload が使う。読むだけ）。
// 出力: 状態(uploaded|uploading|pending)<TAB>ローカルの状態(Current|Downloaded|NotDownloaded|-)<TAB>エラー<TAB>パス
// ビルド: swiftc -O tools/icstat.swift -o ~/.local/share/stor/bin/icstat（stor offload が無ければ自動でビルドする）
import Foundation
for p in CommandLine.arguments.dropFirst() {
  let u = URL(fileURLWithPath: p)
  let v = try? u.resourceValues(forKeys: [.ubiquitousItemIsUploadedKey, .ubiquitousItemIsUploadingKey, .ubiquitousItemDownloadingStatusKey, .ubiquitousItemUploadingErrorKey])
  let state = v?.ubiquitousItemIsUploaded == true ? "uploaded" : (v?.ubiquitousItemIsUploading == true ? "uploading" : "pending")
  let local = v?.ubiquitousItemDownloadingStatus?.rawValue.replacingOccurrences(of: "NSURLUbiquitousItemDownloadingStatus", with: "") ?? "-"
  let err = (v?.ubiquitousItemUploadingError?.localizedDescription ?? "").replacingOccurrences(of: "\t", with: " ").replacingOccurrences(of: "\n", with: " ")
  print("\(state)\t\(local)\t\(err)\t\(p)")
}
