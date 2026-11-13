package streams

import (
 "errors"
 "sync/atomic"
 "testing"
 "time"

 "github.com/AlexxIT/go2rtc/pkg/core"
)

type setupConsumer struct{}
func (*setupConsumer) GetMedias() []*core.Media {
 return []*core.Media{{Kind: core.KindVideo, Direction: core.DirectionSendonly, Codecs: []*core.Codec{{Name: core.CodecH264}}}}
}
func (*setupConsumer) AddTrack(*core.Media, *core.Codec, *core.Receiver) error { return nil }
func (*setupConsumer) Stop() error { return nil }

func TestRetainedSetupAdmissionAndSharedFailure(t *testing.T) {
 release := make(chan struct{})
 var attempts atomic.Int32
 HandleFunc("boundedtest", func(string) (core.Producer, error) {
  attempts.Add(1)
  <-release
  return nil, errors.New("synthetic upstream unavailable")
 })
 defer delete(handlers, "boundedtest")
 stream := NewStream("boundedtest:offline")
 done := make(chan error, maxConsumerSetups)
 for i := 0; i < maxConsumerSetups; i++ {
  go func() { done <- stream.AddConsumer(&setupConsumer{}) }()
 }
 deadline := time.Now().Add(time.Second)
 for stream.setups.Load() < maxConsumerSetups && time.Now().Before(deadline) {
  time.Sleep(time.Millisecond)
 }
 if stream.setups.Load() != maxConsumerSetups {
  close(release)
  t.Fatal("setup requests did not enter")
 }
 // Additional clients must return without waiting for the slow network holder.
 rejected := make(chan error, 1)
 go func() { rejected <- stream.AddConsumer(&setupConsumer{}) }()
 select {
 case err := <-rejected:
  if !errors.Is(err, errSetupBusy) { t.Errorf("admission error = %v", err) }
 case <-time.After(time.Second):
  close(release)
  t.Fatal("setup admission queued another disconnected client")
 }
 close(release)
 for i := 0; i < maxConsumerSetups; i++ {
  select {
  case err := <-done:
   if err == nil { t.Error("unavailable source accepted") }
  case <-time.After(time.Second): t.Fatal("failed callers did not drain")
  }
 }
 if got := attempts.Load(); got != 1 { t.Errorf("shared failure performed %d dials", got) }
 if stream.setups.Load() != 0 || stream.pending.Load() != 0 {
  t.Fatal("setup accounting was retained")
 }
}

func TestFailedDialRetriesAndAddressChange(t *testing.T) {
 var attempts int
 HandleFunc("retrytest", func(string) (core.Producer, error) {
  attempts++
  return nil, errors.New("synthetic unavailable")
 })
 defer delete(handlers, "retrytest")
 p := NewProducer("retrytest:first")
 for i := 0; i < 100; i++ { _ = p.Dial() }
 if attempts != 1 { t.Fatalf("failure was not shared: %d", attempts) }
 // Source replacement must not inherit another address's cooldown.
 p.SetSource("retrytest:second")
 _ = p.Dial()
 if attempts != 2 { t.Fatal("new address inherited stale failure") }
 // Expiration must admit a new attempt without a relay restart.
 p.retryAfter = time.Now().Add(-time.Second)
 _ = p.Dial()
 if attempts != 3 { t.Fatal("expired cooldown did not retry") }
}
