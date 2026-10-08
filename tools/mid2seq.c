#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// ============================================================================
// Byte Order Helpers
// ============================================================================

// Swap byte order for 16-bit integers.
static inline uint16_t swap16(uint16_t val) {
  return (val << 8) | (val >> 8);
}

// Swap byte order for 32-bit integers.
static inline uint32_t swap32(uint32_t val) {
  return (val << 24) | ((val << 8) & 0x00ff0000) | ((val >> 8) & 0x0000ff00) |
         (val >> 24);
}

// ============================================================================
// MIDI 1 to 0 Conversion Engine (In-Memory)
// ============================================================================

static const unsigned long int FCC_MTHD = 0x6468544D; // "MThd" in little-endian
static const unsigned long int FCC_MTRK = 0x6B72544D; // "MTrk" in little-endian

typedef struct midi_track_info {
  unsigned long int TrkBase;
  unsigned long int TrkEnd;
  unsigned long int CurPos;
  unsigned long int TickPos;
  unsigned char LastEvent; // all values are possible
  unsigned char RmbrEvent; // must not be F0 .. FF
} MIDITRK_INF;

static unsigned long int ReadMIDIValue(unsigned char *FileData,
                                      unsigned long int *Value) {
  unsigned char *DataPnt = FileData;
  unsigned long int TempLng = 0;

  while (*DataPnt & 0x80) {
    TempLng <<= 7;
    TempLng |= *DataPnt & 0x7F;
    DataPnt++;
  }
  TempLng <<= 7;
  TempLng |= *DataPnt & 0x7F;
  DataPnt++;

  *Value = TempLng;
  return (unsigned long int)(DataPnt - FileData);
}

static unsigned long int WriteMIDIValue(unsigned char *FileData,
                                       unsigned long int Value) {
  unsigned char *DataPnt;
  unsigned char ByteCount = 0;
  unsigned long int TempLng = Value;

  do {
    TempLng >>= 7;
    ByteCount++;
  } while (TempLng);

  TempLng = Value;
  DataPnt = FileData + (ByteCount - 1);
  *DataPnt = (unsigned char)(TempLng & 0x7F);
  TempLng >>= 7;

  while (TempLng) {
    DataPnt--;
    *DataPnt = 0x80 | (unsigned char)(TempLng & 0x7F);
    TempLng >>= 7;
  }

  return ByteCount;
}

static unsigned char CopyMIDIEvent(unsigned char *SrcData, unsigned char *DstData,
                                  MIDITRK_INF *TrkSrc, MIDITRK_INF *TrkDst) {
  bool ShortEvt;
  unsigned long int DataLen;
  unsigned long int TempLng;

  if (SrcData[TrkSrc->CurPos] & 0x80) {
    TrkSrc->LastEvent = SrcData[TrkSrc->CurPos];
    if ((SrcData[TrkSrc->CurPos] & 0xF0) < 0xF0)
      TrkSrc->RmbrEvent = SrcData[TrkSrc->CurPos];
    TrkSrc->CurPos++;
    ShortEvt = false;
  } else {
    if (!TrkSrc->RmbrEvent)
      return 0xFF;
    TrkSrc->LastEvent = TrkSrc->RmbrEvent; // not valid to have short F? events
    ShortEvt = true;
  }

  if (!ShortEvt || (ShortEvt && TrkDst->LastEvent != TrkSrc->LastEvent)) {
    DstData[TrkDst->CurPos] = TrkSrc->LastEvent;
    TrkDst->LastEvent = TrkSrc->LastEvent;
    TrkDst->CurPos++;
  }

  switch (TrkSrc->LastEvent & 0xF0) {
  case 0x80:
  case 0x90:
  case 0xA0:
  case 0xB0:
  case 0xE0:
    // Copy 2 Bytes
    memcpy(&DstData[TrkDst->CurPos], &SrcData[TrkSrc->CurPos], 0x02);
    TrkSrc->CurPos += 0x02;
    TrkDst->CurPos += 0x02;
    break;
  case 0xC0:
  case 0xD0:
    // Copy 1 Byte
    DstData[TrkDst->CurPos] = SrcData[TrkSrc->CurPos];
    TrkSrc->CurPos += 0x01;
    TrkDst->CurPos += 0x01;
    break;
  case 0xF0:
    switch (TrkSrc->LastEvent) {
    case 0xF0:
    case 0xF7: // SysEx / Escape Data
      TempLng = ReadMIDIValue(SrcData + TrkSrc->CurPos, &DataLen);
      DataLen += TempLng;
      memcpy(&DstData[TrkDst->CurPos], &SrcData[TrkSrc->CurPos], DataLen);
      TrkSrc->CurPos += DataLen;
      TrkDst->CurPos += DataLen;
      break;
    case 0xFF: // Meta Event
      // Check & Copy Meta Event ID
      switch (SrcData[TrkSrc->CurPos]) {
      case 0x20:
      case 0x21:
      case 0x2F:
        // Ignore Event
        TrkSrc->CurPos += 0x01;
        TempLng = ReadMIDIValue(SrcData + TrkSrc->CurPos, &DataLen);
        TrkSrc->CurPos += DataLen + TempLng;
        TrkDst->CurPos -= 0x01;
        return 0x01;
      }
      DstData[TrkDst->CurPos] = SrcData[TrkSrc->CurPos];
      TrkSrc->CurPos += 0x01;
      TrkDst->CurPos += 0x01;
      // Copy Meta Event Data
      TempLng = ReadMIDIValue(SrcData + TrkSrc->CurPos, &DataLen);
      DataLen += TempLng;
      memcpy(&DstData[TrkDst->CurPos], &SrcData[TrkSrc->CurPos], DataLen);
      TrkSrc->CurPos += DataLen;
      TrkDst->CurPos += DataLen;
      break;
    }
    break;
  }

  return 0x00;
}

static unsigned char MIDI1to0(unsigned long int SrcLen, unsigned char *SrcData,
                              unsigned long int *RetDstLen,
                              unsigned char **RetDstData) {
  unsigned long int CurPos;
  unsigned short int TrkCnt;
  unsigned short int CurTrk;
  MIDITRK_INF MstTrk;
  MIDITRK_INF *TrkData;
  MIDITRK_INF *CurTData;
  unsigned long int DstLen;
  unsigned char *DstData;
  unsigned long int DataLen;
  unsigned long int LastTick;
  unsigned char TempByt;
  unsigned short int TempSht;
  unsigned long int TempLng;
  bool WriteDelay;
  // Last Delay Backup Values
  unsigned long int LD_Pos = 0;
  unsigned long int LD_Tick = 0;
  unsigned long int EvtsWritten;

  if (SrcLen < 14)
    return 0xF0;

  CurPos = 0x00;
  memcpy(&TempLng, &SrcData[CurPos + 0x00], 0x04);
  if (TempLng != FCC_MTHD)
    return 0xF0;
  memcpy(&TempLng, &SrcData[CurPos + 0x04], 0x04);
  DataLen = swap32(TempLng);

  DstLen = SrcLen * 2 + 65536;
  DstData = (unsigned char *)malloc(DstLen);
  if (!DstData)
    return 0xF1;

  memcpy(&DstData[CurPos + 0x00], &FCC_MTHD, 0x04);
  memcpy(&DstData[CurPos + 0x04], &TempLng, 0x04);
  CurPos += 0x08;

  // Read Header
  memcpy(&TrkCnt, &SrcData[CurPos + 0x02], 0x02);
  TrkCnt = swap16(TrkCnt);
  // Write MIDI Format 0
  TempSht = swap16(0x0000);
  memcpy(&DstData[CurPos + 0x00], &TempSht, 0x02);
  // Write Track Count 1
  TempSht = swap16(0x0001);
  memcpy(&DstData[CurPos + 0x02], &TempSht, 0x02);
  // Write Resolution Rate (and other bytes, if used)
  if (DataLen > 4) {
    memcpy(&DstData[CurPos + 0x04], &SrcData[CurPos + 0x04], DataLen - 0x04);
  }
  CurPos += DataLen;

  TrkData = (MIDITRK_INF *)malloc(TrkCnt * sizeof(MIDITRK_INF));
  if (!TrkData) {
    free(DstData);
    return 0xF1;
  }

  for (CurTrk = 0x00; CurTrk < TrkCnt; CurTrk++) {
    if (CurPos + 8 > SrcLen) {
      free(TrkData);
      free(DstData);
      return 0xE0;
    }
    memcpy(&TempLng, &SrcData[CurPos + 0x00], 0x04);
    if (TempLng != FCC_MTRK) {
      free(TrkData);
      free(DstData);
      return 0xE0;
    }
    memcpy(&DataLen, &SrcData[CurPos + 0x04], 0x04);
    DataLen = swap32(DataLen);

    TrkData[CurTrk].TrkBase = CurPos;
    TrkData[CurTrk].TrkEnd = CurPos + 0x08 + DataLen;
    CurPos += 0x08 + DataLen;
  }

  MstTrk.TrkBase = TrkData[0x00].TrkBase;
  MstTrk.CurPos = MstTrk.TrkBase;
  memcpy(&DstData[MstTrk.CurPos + 0x00], &FCC_MTRK, 0x04);
  TempLng = 0x00000000;
  memcpy(&DstData[MstTrk.CurPos + 0x04], &TempLng, 0x04);
  MstTrk.CurPos += 0x08;
  MstTrk.TickPos = 0x00000000;
  MstTrk.LastEvent = 0x00;
  MstTrk.RmbrEvent = 0x00;

  for (CurTrk = 0x00; CurTrk < TrkCnt; CurTrk++) {
    CurTData = TrkData + CurTrk;
    CurTData->CurPos = CurTData->TrkBase + 0x08;
    CurTData->TickPos = 0x00000000;
    CurTData->LastEvent = 0x00;
    CurTData->RmbrEvent = 0x00;

    ReadMIDIValue(SrcData + CurTData->CurPos, &TempLng);
    CurTData->TickPos += TempLng;
  }

  LastTick = 0x00000000;
  MstTrk.TickPos = 0x00000000;
  do {
    // Search Next Event
    TempLng = MstTrk.TickPos;
    TempSht = 0x0000;
    for (CurTrk = 0x00; CurTrk < TrkCnt; CurTrk++) {
      CurTData = TrkData + CurTrk;
      if (CurTData->CurPos >= CurTData->TrkEnd)
        continue;
      if (!TempSht || CurTData->TickPos < TempLng)
        TempLng = CurTData->TickPos;
      TempSht++;
    }
    MstTrk.TickPos = TempLng;

    if (MstTrk.CurPos + 65536 >= DstLen) {
      unsigned long int NewDstLen = DstLen * 2 + 65536;
      unsigned char *NewDst = (unsigned char *)realloc(DstData, NewDstLen);
      if (!NewDst) {
        free(TrkData);
        free(DstData);
        return 0xF1;
      }
      DstData = NewDst;
      DstLen = NewDstLen;
    }

    LD_Pos = MstTrk.CurPos;
    LD_Tick = LastTick;
    DataLen = WriteMIDIValue(DstData + MstTrk.CurPos, MstTrk.TickPos - LastTick);
    LastTick = MstTrk.TickPos;
    MstTrk.CurPos += DataLen;

    // Write Events
    TempSht = 0x0000;
    EvtsWritten = 0x00000000;
    WriteDelay = false;
    for (CurTrk = 0x00; CurTrk < TrkCnt; CurTrk++) {
      CurTData = TrkData + CurTrk;
      if (CurTData->CurPos >= CurTData->TrkEnd) {
        TempSht++;
        continue;
      }
      if (CurTData->TickPos > MstTrk.TickPos)
        continue;

      ReadMIDIValue(SrcData + CurTData->CurPos, &TempLng);
      CurTData->TickPos -= TempLng;
      while (CurTData->CurPos < CurTData->TrkEnd) {
        DataLen = ReadMIDIValue(SrcData + CurTData->CurPos, &TempLng);
        CurTData->TickPos += TempLng;
        if (CurTData->TickPos > MstTrk.TickPos)
          break;
        CurTData->CurPos += DataLen;

        if (MstTrk.CurPos + 65536 >= DstLen) {
          unsigned long int NewDstLen = DstLen * 2 + 65536;
          unsigned char *NewDst = (unsigned char *)realloc(DstData, NewDstLen);
          if (!NewDst) {
            free(TrkData);
            free(DstData);
            return 0xF1;
          }
          DstData = NewDst;
          DstLen = NewDstLen;
        }

        if (WriteDelay) {
          LD_Pos = MstTrk.CurPos;
          LD_Tick = LastTick;
          DataLen = WriteMIDIValue(DstData + MstTrk.CurPos, 0);
          MstTrk.CurPos += DataLen;
          WriteDelay = false;
        }
        TempByt = CopyMIDIEvent(SrcData, DstData, CurTData, &MstTrk);
        switch (TempByt) {
        case 0x00:
          EvtsWritten++;
          WriteDelay = true;
          break;
        case 0x01: // Event ignored
          break;
        case 0xFF: // Error
        default:
          printf("Invalid Event at Pos %lX\n", CurTData->CurPos);
          free(TrkData);
          free(DstData);
          return 0xFF;
        }
      }
      if (CurTData->CurPos >= CurTData->TrkEnd)
        TempSht++;
    }

    if (!WriteDelay && (TempSht < TrkCnt)) {
      MstTrk.CurPos = LD_Pos;
      LastTick = LD_Tick;
    }
  } while (TempSht < TrkCnt);

  if (MstTrk.CurPos + 16 >= DstLen) {
    unsigned long int NewDstLen = DstLen + 65536;
    unsigned char *NewDst = (unsigned char *)realloc(DstData, NewDstLen);
    if (!NewDst) {
      free(TrkData);
      free(DstData);
      return 0xF1;
    }
    DstData = NewDst;
    DstLen = NewDstLen;
  }

  // Write Track End
  if (WriteDelay) {
    DstData[MstTrk.CurPos + 0x00] = 0x00;
    MstTrk.CurPos += 0x01;
  }
  DstData[MstTrk.CurPos + 0x00] = 0xFF;
  DstData[MstTrk.CurPos + 0x01] = 0x2F;
  DstData[MstTrk.CurPos + 0x02] = 0x00;
  MstTrk.CurPos += 0x03;

  MstTrk.TrkEnd = MstTrk.CurPos;
  TempLng = swap32(MstTrk.TrkEnd - MstTrk.TrkBase - 0x08);
  memcpy(&DstData[MstTrk.TrkBase + 0x04], &TempLng, 0x04);
  DstLen = MstTrk.TrkEnd;

  free(TrkData);

  *RetDstLen = DstLen;
  *RetDstData = DstData;

  return 0x00;
}

// ============================================================================
// In-Memory MIDI Stream Reader
// ============================================================================

typedef struct {
  const uint8_t *data;
  size_t size;
  size_t pos;
} MidiMemReader;

static inline int mem_read_byte(MidiMemReader *r) {
  if (r->pos < r->size)
    return r->data[r->pos++];
  return EOF;
}

static inline void mem_unread_byte(MidiMemReader *r) {
  if (r->pos > 0)
    r->pos--;
}

static inline size_t mem_read_bytes(MidiMemReader *r, void *dest, size_t count) {
  if (r->pos + count > r->size)
    count = (r->pos <= r->size) ? (r->size - r->pos) : 0;
  if (count > 0) {
    memcpy(dest, r->data + r->pos, count);
    r->pos += count;
  }
  return count;
}

static inline void mem_skip_bytes(MidiMemReader *r, size_t count) {
  if (r->pos + count <= r->size)
    r->pos += count;
  else
    r->pos = r->size;
}

static uint32_t read_variable_length_mem(MidiMemReader *r) {
  uint32_t value = 0;
  int byte;

  if (r->pos >= r->size)
    return 0;
  byte = mem_read_byte(r);
  if (byte == EOF)
    return 0;
  value = (uint8_t)byte & 0x7F;

  while ((uint8_t)byte & 0x80) {
    if (r->pos >= r->size)
      return value;
    byte = mem_read_byte(r);
    if (byte == EOF)
      return value;
    value = (value << 7) | ((uint8_t)byte & 0x7F);
  }
  return value;
}

// ============================================================================
// SEQ Structures and Helpers
// ============================================================================

// Structure to hold SEQ file header information.
// The SEQ format is Big Endian.
typedef struct {
  uint16_t resolution;
  uint16_t num_tempo_events;
  uint16_t data_offset;
  uint16_t tempo_loop_offset;
} SeqHeader;

// Structure for tempo events in the SEQ file.
typedef struct {
  uint32_t step_time; // Delta time from previous tempo event
  uint32_t mspb;      // Microseconds per beat
} SeqTempoEvent;

// Structure to hold a MIDI event after being read from the file.
// This allows us to process all events before writing the final SEQ file.
typedef struct {
  uint32_t absolute_time;
  uint8_t status;
  uint8_t data1;
  uint8_t data2;
  uint32_t gate_time; // Calculated for Note On events
  uint32_t order;     // Original file order, keeps the sort stable
} TrackEvent;

// Comparison function for qsort to sort events by their absolute time.
// This is crucial because MIDI events at the same timestamp are not guaranteed
// to be in order.
static int compare_events(const void *a, const void *b) {
  const TrackEvent *eventA = (const TrackEvent *)a;
  const TrackEvent *eventB = (const TrackEvent *)b;
  if (eventA->absolute_time < eventB->absolute_time)
    return -1;
  if (eventA->absolute_time > eventB->absolute_time)
    return 1;
  // For events at the same time, ensure Note Off events come first
  // to handle zero-duration notes correctly during gate calculation.
  uint8_t typeA = eventA->status & 0xF0;
  uint8_t typeB = eventB->status & 0xF0;
  if ((typeA == 0x80 || (typeA == 0x90 && eventA->data2 == 0)) &&
      (typeB != 0x80 && (typeB != 0x90 || eventB->data2 != 0)))
    return -1;
  if ((typeB == 0x80 || (typeB == 0x90 && eventB->data2 == 0)) &&
      (typeA != 0x80 && (typeA != 0x90 || eventA->data2 != 0)))
    return 1;
  // qsort is not stable: fall back to file order so e.g. a program change
  // stays in front of the note it was meant for.
  if (eventA->order < eventB->order)
    return -1;
  if (eventA->order > eventB->order)
    return 1;
  return 0;
}

// ============================================================================
// Voice Map (from "sf2ton.py --all-banks")
// ============================================================================
// Maps (SF2 bank, MIDI program) -> TON voice so a single merged TON bank can
// hold every SF2 bank, including drum kits (SF2 bank 128).

#define VMAP_DRUM_BANK 128
static int16_t g_vmap[VMAP_DRUM_BANK + 1][128];
static bool g_have_vmap = false;

static bool load_voice_map(const char *path) {
  FILE *f = fopen(path, "r");
  if (!f) {
    perror("Error opening voice map");
    return false;
  }
  for (int b = 0; b <= VMAP_DRUM_BANK; b++)
    for (int p = 0; p < 128; p++)
      g_vmap[b][p] = -1;

  char line[512];
  int entries = 0;
  while (fgets(line, sizeof(line), f)) {
    int bank, prog, voice;
    if (line[0] == '#')
      continue;
    if (sscanf(line, "%d %d %d", &bank, &prog, &voice) != 3)
      continue;
    if (bank < 0 || prog < 0 || prog > 127 || voice < 0 || voice > 127)
      continue;
    if (bank > VMAP_DRUM_BANK)
      bank = VMAP_DRUM_BANK;
    if (g_vmap[bank][prog] < 0) {
      g_vmap[bank][prog] = (int16_t)voice;
      entries++;
    }
  }
  fclose(f);
  if (!entries) {
    printf("Error: voice map %s has no entries.\n", path);
    return false;
  }
  printf("Loaded voice map %s (%d entries)\n", path, entries);
  g_have_vmap = true;
  return true;
}

// Melodic lookup: exact bank -> bank 0 -> any melodic bank -> bank 0 prog 0.
static int map_melodic(int bank, int prog) {
  if (bank >= 0 && bank < VMAP_DRUM_BANK && g_vmap[bank][prog] >= 0)
    return g_vmap[bank][prog];
  if (g_vmap[0][prog] >= 0)
    return g_vmap[0][prog];
  for (int b = 1; b < VMAP_DRUM_BANK; b++)
    if (g_vmap[b][prog] >= 0)
      return g_vmap[b][prog];
  if (g_vmap[0][0] >= 0)
    return g_vmap[0][0];
  for (int b = 0; b <= VMAP_DRUM_BANK; b++)
    for (int p = 0; p < 128; p++)
      if (g_vmap[b][p] >= 0)
        return g_vmap[b][p];
  return 0;
}

// Drum lookup: kit <prog> -> standard kit 0 -> any kit -> melodic fallback.
static int map_drum(int prog) {
  if (g_vmap[VMAP_DRUM_BANK][prog] >= 0)
    return g_vmap[VMAP_DRUM_BANK][prog];
  if (g_vmap[VMAP_DRUM_BANK][0] >= 0)
    return g_vmap[VMAP_DRUM_BANK][0];
  for (int p = 0; p < 128; p++)
    if (g_vmap[VMAP_DRUM_BANK][p] >= 0)
      return g_vmap[VMAP_DRUM_BANK][p];
  return map_melodic(0, prog);
}

// GM: channel 10 is drums. GM2 (MSB 120) and XG (MSB 127) drum banks too.
static bool is_drum_part(int channel, int bank_msb) {
  return channel == 9 || bank_msb == 120 || bank_msb == 127;
}

static int map_program(int channel, int bank_msb, int prog) {
  return is_drum_part(channel, bank_msb) ? map_drum(prog)
                                         : map_melodic(bank_msb, prog);
}

// ============================================================================
// GM Mode ("-g", pairs with "sf2ton.py --gm-banks")
// ============================================================================
// Fixed layout, no voice map: tone bank 1 holds the 128 GM melodic programs,
// GM_DRUM_TONE_BANK holds the drum kits (voice = kit program).  Drum parts get
// CC#32 = GM_DRUM_TONE_BANK, so the SEQ plays with any TON built that way.
// Must match GM_DRUM_TONE_BANK in sf2ton.py.
#define GM_DRUM_TONE_BANK 2
static bool g_gm_mode = false;
// Optional workarounds (see usage text); both off by default.
static bool g_expand_resets = false; // -r
static bool g_restate_bank = false;  // -B

static uint8_t gm_initial_tone_bank(int channel) {
  return channel == 9 ? GM_DRUM_TONE_BANK : 1;
}

// Writes Step(Delta) Extend events (0x8D-0x8F) for any event type.
// These handle the largest chunks of time.
static void write_large_delta_events(FILE *file, uint32_t *delta) {
  while (*delta >= 0x1000) {
    fputc(0x8F, file);
    *delta -= 0x1000;
  }
  while (*delta >= 0x800) {
    fputc(0x8E, file);
    *delta -= 0x800;
  }
  while (*delta >= 0x200) {
    fputc(0x8D, file);
    *delta -= 0x200;
  }
}

// Writes Gate Extend events (0x88-0x8B) for Note On events.
static void write_extended_gate(FILE *file, uint32_t *gate) {
  while (*gate >= 0x2000) {
    fputc(0x8B, file);
    *gate -= 0x2000;
  }
  while (*gate >= 0x1000) {
    fputc(0x8A, file);
    *gate -= 0x1000;
  }
  while (*gate >= 0x800) {
    fputc(0x89, file);
    *gate -= 0x800;
  }
  while (*gate >= 0x200) {
    fputc(0x88, file);
    *gate -= 0x200;
  }
}

// ============================================================================
// Main Program
// ============================================================================

int main(int argc, char *argv[]) {
  const char *vmap_path = NULL;
  const char *in_path = NULL;
  const char *out_path = NULL;
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "-m") == 0 && i + 1 < argc) {
      vmap_path = argv[++i];
    } else if (strcmp(argv[i], "-g") == 0) {
      g_gm_mode = true;
    } else if (strcmp(argv[i], "-r") == 0) {
      g_expand_resets = true;
    } else if (strcmp(argv[i], "-B") == 0) {
      g_restate_bank = true;
    } else if (!in_path) {
      in_path = argv[i];
    } else if (!out_path) {
      out_path = argv[i];
    } else {
      in_path = NULL; // too many arguments
      break;
    }
  }
  if (!in_path || !out_path || (vmap_path && g_gm_mode)) {
    printf("Usage: %s [-g | -m voices.vmap] [-r] [-B] <input.mid> <output.seq>\n", argv[0]);
    printf("  -g  GM mode, for TONs from 'sf2ton.py --gm-banks': melodic parts use\n");
    printf("      tone bank 1, drum parts (channel 10, MSB 120/127) tone bank %d\n",
           GM_DRUM_TONE_BANK);
    printf("  -m  voice map from 'sf2ton.py --all-banks': rewrites bank select +\n");
    printf("      program changes (and channel 10 drums) to merged TON voices\n");
    printf("  -r  replace Reset All Controllers (CC#121) with explicit resets\n");
    printf("      (try if a song plays all piano)\n");
    printf("  -B  re-send the tone bank (CC#32) before every program change\n");
    printf("  -r and -B are workarounds that fix some songs and break others\n");
    return 1;
  }
  if (vmap_path && !load_voice_map(vmap_path))
    return 1;

  FILE *midi_file = fopen(in_path, "rb");
  if (!midi_file) {
    perror("Error opening MIDI file");
    return 1;
  }

  fseek(midi_file, 0, SEEK_END);
  long input_size = ftell(midi_file);
  fseek(midi_file, 0, SEEK_SET);

  if (input_size < 14) {
    printf("Error: File is too small to be a valid MIDI file.\n");
    fclose(midi_file);
    return 1;
  }

  unsigned char *input_data = (unsigned char *)malloc(input_size);
  if (!input_data) {
    printf("Failed to allocate memory for input MIDI file.\n");
    fclose(midi_file);
    return 1;
  }

  if (fread(input_data, 1, input_size, midi_file) != (size_t)input_size) {
    perror("Error reading MIDI file");
    free(input_data);
    fclose(midi_file);
    return 1;
  }
  fclose(midi_file);

  if (memcmp(input_data, "MThd", 4) != 0) {
    printf("Error: Not a valid MIDI file (missing MThd header).\n");
    free(input_data);
    return 1;
  }

  uint16_t in_format = ((uint16_t)input_data[8] << 8) | input_data[9];
  unsigned char *fmt0_data = NULL;
  unsigned long int fmt0_size = 0;
  bool is_converted_fmt0 = false;

  if (in_format == 0) {
    fmt0_data = input_data;
    fmt0_size = (unsigned long int)input_size;
  } else if (in_format == 1) {
    printf("Converting MIDI 1 to MIDI 0 in memory...\n");
    unsigned char res =
        MIDI1to0((unsigned long int)input_size, input_data, &fmt0_size, &fmt0_data);
    if (res != 0x00) {
      printf("Error: MIDI 1 to 0 conversion failed with error code 0x%02X\n", res);
      free(input_data);
      return 1;
    }
    is_converted_fmt0 = true;
  } else {
    printf("This program only supports MIDI format 0 and format 1.\n");
    free(input_data);
    return 1;
  }

  MidiMemReader reader = {fmt0_data, (size_t)fmt0_size, 0};

  // Read MIDI header chunk
  char header_id[4];
  uint32_t header_length;
  uint16_t format;
  uint16_t num_tracks;
  uint16_t division;

  mem_read_bytes(&reader, header_id, 4);
  mem_read_bytes(&reader, &header_length, 4);
  header_length = swap32(header_length);
  mem_read_bytes(&reader, &format, 2);
  format = swap16(format);
  mem_read_bytes(&reader, &num_tracks, 2);
  num_tracks = swap16(num_tracks);
  mem_read_bytes(&reader, &division, 2);
  division = swap16(division);

  if (header_length > 6) {
    mem_skip_bytes(&reader, header_length - 6);
  }

  if (format != 0) {
    printf("This program only supports MIDI format 0.\n");
    if (is_converted_fmt0)
      free(fmt0_data);
    free(input_data);
    return 1;
  }

  // Read MIDI track chunk header
  char track_id[4];
  uint32_t track_length;
  mem_read_bytes(&reader, track_id, 4);
  mem_read_bytes(&reader, &track_length, 4);
  track_length = swap32(track_length);
  size_t track_start_pos = reader.pos;

  // === PASS 1: Read all MIDI events into an in-memory array ===
  size_t capacity = (size_t)track_length + 256;
  TrackEvent *events = (TrackEvent *)malloc(sizeof(TrackEvent) * capacity);
  if (!events) {
    printf("Failed to allocate memory for events.\n");
    if (is_converted_fmt0)
      free(fmt0_data);
    free(input_data);
    return 1;
  }
  int event_count = 0;

  // Tempo map: absolute tick + microseconds per beat, in file order.
  // (Converted to SEQ tempo events after all events are read.)
  typedef struct {
    uint32_t time;
    uint32_t mspb;
  } TempoPoint;
  size_t tempo_cap = 64;
  int tempo_count = 0;
  TempoPoint *tempo_points = (TempoPoint *)malloc(sizeof(TempoPoint) * tempo_cap);
  if (!tempo_points) {
    printf("Failed to allocate memory for tempo map.\n");
    return 1;
  }

  uint8_t last_status = 0;
  uint32_t current_time = 0;

  while (reader.pos < track_start_pos + track_length && reader.pos < reader.size) {
    uint32_t delta_time = read_variable_length_mem(&reader);
    current_time += delta_time;

    int byte = mem_read_byte(&reader);
    if (byte == EOF)
      break;
    uint8_t status = (uint8_t)byte;
    if ((status & 0x80) == 0) { // Running status
      mem_unread_byte(&reader);
      status = last_status;
    }

    if ((size_t)event_count >= capacity) {
      capacity *= 2;
      TrackEvent *new_events =
          (TrackEvent *)realloc(events, sizeof(TrackEvent) * capacity);
      if (!new_events) {
        printf("Failed to reallocate memory for events.\n");
        free(events);
        if (is_converted_fmt0)
          free(fmt0_data);
        free(input_data);
        return 1;
      }
      events = new_events;
    }

    TrackEvent *current_event = &events[event_count];
    current_event->absolute_time = current_time;
    current_event->status = status;
    current_event->gate_time = 0;
    current_event->order = (uint32_t)event_count;

    uint8_t event_type = status & 0xF0;

    switch (event_type) {
    case 0x90:
    case 0x80:
    case 0xB0:
    case 0xA0:
    case 0xE0:
      current_event->data1 = (uint8_t)mem_read_byte(&reader);
      current_event->data2 = (uint8_t)mem_read_byte(&reader);
      event_count++;
      break;

    case 0xC0:
    case 0xD0:
      current_event->data1 = (uint8_t)mem_read_byte(&reader);
      current_event->data2 = 0;
      event_count++;
      break;

    case 0xF0:
      if (status == 0xFF) { // Meta Event
        uint8_t meta_type = (uint8_t)mem_read_byte(&reader);
        uint32_t length = read_variable_length_mem(&reader);
        if (meta_type == 0x51 && length == 3) { // Set Tempo
          uint32_t mspb = 0;
          for (uint32_t i = 0; i < length; ++i)
            mspb = (mspb << 8) | (uint8_t)mem_read_byte(&reader);
          if ((size_t)tempo_count >= tempo_cap) {
            tempo_cap *= 2;
            TempoPoint *nt =
                (TempoPoint *)realloc(tempo_points, sizeof(TempoPoint) * tempo_cap);
            if (!nt) {
              printf("Failed to allocate memory for tempo map.\n");
              return 1;
            }
            tempo_points = nt;
          }
          if (mspb > 0) {
            tempo_points[tempo_count].time = current_time;
            tempo_points[tempo_count].mspb = mspb;
            tempo_count++;
          }
        } else {
          mem_skip_bytes(&reader, length);
        }
      } else if (status == 0xF0 || status == 0xF7) { // SysEx: not supported, skip
        uint32_t length = read_variable_length_mem(&reader);
        mem_skip_bytes(&reader, length);
      }
      continue; // system/meta events don't set running status
    }
    last_status = status;
  }

  // === PASS 1b: Fit the timebase into what the driver accepts ===
  // Sega: "The timebases supported by Sega Saturn are between 24 and 960"
  // (out of range -> sequence status 80h, no playback).  Rescale all times
  // by a power of two; absolute times are rounded so nothing drifts.
  uint32_t resolution = division;
  if (division & 0x8000) {
    // SMPTE timing: ticks per second = fps * ticks_per_frame.  Express it as
    // a fixed 120 BPM (500000 us/beat) with ticks-per-beat = tps / 2.
    int fps = -(int8_t)(division >> 8);
    int tpf = division & 0xFF;
    uint32_t tps = (uint32_t)(fps == 29 ? 30 : fps) * (uint32_t)tpf;
    resolution = tps / 2 ? tps / 2 : 1;
    tempo_count = 0; // tempo metas don't apply to SMPTE time
    printf("SMPTE timing (%d fps x %d): using resolution %u at 120 BPM.\n",
           fps, tpf, resolution);
  }
  int res_shift = 0; // >0: divide times by 2^n, <0: multiply by 2^-n
  while ((resolution >> res_shift) > 960)
    res_shift++;
  if (res_shift == 0 && resolution > 0)
    while ((resolution << -res_shift) < 24)
      res_shift--;
  if (res_shift != 0) {
    uint32_t new_res =
        res_shift > 0 ? resolution >> res_shift : resolution << -res_shift;
    printf("Resolution %u is outside the driver's 24-960 range; rescaling to %u.\n",
           resolution, new_res);
    for (int i = 0; i < event_count; i++) {
      uint32_t t = events[i].absolute_time;
      events[i].absolute_time =
          res_shift > 0 ? (t + (1u << (res_shift - 1))) >> res_shift : t << -res_shift;
    }
    for (int i = 0; i < tempo_count; i++) {
      uint32_t t = tempo_points[i].time;
      tempo_points[i].time =
          res_shift > 0 ? (t + (1u << (res_shift - 1))) >> res_shift : t << -res_shift;
    }
    resolution = new_res;
  }

  // === PASS 2: Calculate gate times ===
  int active_note_indices[16][128];
  for (int i = 0; i < 16; i++)
    for (int j = 0; j < 128; j++)
      active_note_indices[i][j] = -1;

  for (int i = 0; i < event_count; i++) {
    uint8_t event_type = events[i].status & 0xF0;
    uint8_t channel = events[i].status & 0x0F;
    uint8_t key = events[i].data1;
    uint8_t velocity = events[i].data2;

    if (event_type == 0x90 && velocity > 0) {
      if (active_note_indices[channel][key] != -1) {
        int prev_idx = active_note_indices[channel][key];
        events[prev_idx].gate_time =
            events[i].absolute_time - events[prev_idx].absolute_time;
      }
      active_note_indices[channel][key] = i;
    } else if (event_type == 0x80 || (event_type == 0x90 && velocity == 0)) {
      int note_on_index = active_note_indices[channel][key];
      if (note_on_index != -1) {
        events[note_on_index].gate_time =
            events[i].absolute_time - events[note_on_index].absolute_time;
        active_note_indices[channel][key] = -1;
      }
      // SEQ has no Note Off: always drop it. An unmatched one (e.g. a second
      // note-off after a retrigger) would otherwise be written as 0x8n, which
      // the driver reads as a gate/step-extend command and desyncs the track.
      events[i].status = 0x00;
    }
  }

  // Notes that never got a note-off: hold them to the end of the song.
  // Zero-length notes (note-off on the same tick, common for drum hits)
  // get the minimum gate of 1 step so the driver still keys them on.
  {
    uint32_t song_end = 0;
    for (int i = 0; i < event_count; i++)
      if (events[i].absolute_time > song_end)
        song_end = events[i].absolute_time;
    for (int ch = 0; ch < 16; ch++)
      for (int key = 0; key < 128; key++) {
        int idx = active_note_indices[ch][key];
        if (idx != -1)
          events[idx].gate_time = song_end - events[idx].absolute_time;
      }
    for (int i = 0; i < event_count; i++)
      if ((events[i].status & 0xF0) == 0x90 && events[i].gate_time == 0)
        events[i].gate_time = 1;
  }

  // === PASS 3: Sort events to ensure correct delta time calculation ===
  qsort(events, event_count, sizeof(TrackEvent), compare_events);

  // === PASS 3b: Bank select / program change handling ===
  // The Saturn driver uses CC#32 to pick the TON bank (we force bank 1 below),
  // so the MIDI's own bank selects (CC#0 / CC#32) must not reach the SEQ:
  // a GM/GS file's "CC#32 = 0" would switch the channel to the driver's
  // internal bank 0.  With a voice map, they are used to resolve programs.
  {
    uint8_t bank_msb[16] = {0};
    uint8_t tone_bank[16];
    int dropped_bank_selects = 0, remapped = 0, bank_switches = 0;
    for (int ch = 0; ch < 16; ch++)
      tone_bank[ch] = g_gm_mode ? gm_initial_tone_bank(ch) : 1;

    // GM mode may insert one CC#32 per program change, and every CC#121 is
    // expanded into 4 explicit resets.
    TrackEvent *out =
        (TrackEvent *)malloc(sizeof(TrackEvent) * (size_t)(event_count * 4 + 1));
    if (!out) {
      printf("Failed to allocate memory for events.\n");
      return 1;
    }
    int out_count = 0;
    int expanded_resets = 0;

    for (int i = 0; i < event_count; i++) {
      uint8_t type = events[i].status & 0xF0;
      uint8_t ch = events[i].status & 0x0F;
      if (type == 0xB0 && events[i].data1 == 121 && g_expand_resets) {
        // -r: Reset All Controllers.  How the Saturn driver handles it is not
        // documented.  Some songs that send it right before their program
        // changes play all piano, which fits the reset
        // clearing the channel's tone bank.  Replace it with the controllers
        // GM (RP-015) says it resets.  Off by default: it regressed others.
        static const uint8_t resets[3][2] = {{1, 0}, {11, 127}, {64, 0}};
        for (int r = 0; r < 3; r++) {
          TrackEvent cc = events[i];
          cc.data1 = resets[r][0];
          cc.data2 = resets[r][1];
          out[out_count++] = cc;
        }
        TrackEvent pb = events[i];
        pb.status = 0xE0 | ch;
        pb.data1 = 0x00;
        pb.data2 = 0x40; // pitch bend centre (SEQ keeps the MSB)
        out[out_count++] = pb;
        expanded_resets++;
        continue;
      }
      if (type == 0xB0 && (events[i].data1 == 0x00 || events[i].data1 == 0x20)) {
        if (events[i].data1 == 0x00)
          bank_msb[ch] = events[i].data2;
        events[i].status = 0x00; // remove
        dropped_bank_selects++;
      } else if (type == 0xC0) {
        if (g_have_vmap)
          events[i].data1 =
              (uint8_t)map_program(ch, bank_msb[ch], events[i].data1 & 0x7F);
        // Melodic parts use tone bank 1, drum parts (GM mode) the drum bank.
        uint8_t want = (g_gm_mode && is_drum_part(ch, bank_msb[ch])) ? GM_DRUM_TONE_BANK : 1;
        // Default: only emit CC#32 when the channel's tone bank changes
        // (GM mode drum <-> melodic).  -B re-states it before every program
        // change (in case something reset the bank); off by default because
        // it regressed some songs.
        if (want != tone_bank[ch] || g_restate_bank) {
          if (want != tone_bank[ch])
            bank_switches++;
          TrackEvent bs = events[i];
          bs.status = 0xB0 | ch;
          bs.data1 = 0x20;
          bs.data2 = want;
          bs.gate_time = 0;
          out[out_count++] = bs;
          tone_bank[ch] = want;
        }
        remapped += g_have_vmap ? 1 : 0;
      }
      out[out_count++] = events[i];
    }
    free(events);
    events = out;
    event_count = out_count;
    if (expanded_resets)
      printf("Replaced %d Reset All Controllers (CC#121) with explicit resets.\n",
             expanded_resets);
    if (dropped_bank_selects)
      printf("Removed %d MIDI bank select events.\n", dropped_bank_selects);
    if (g_have_vmap)
      printf("Remapped %d program changes through the voice map.\n", remapped);
    if (g_gm_mode)
      printf("GM mode: %d drum/melodic tone bank switches inserted.\n", bank_switches);
  }

  // === PASS 4: Build the SEQ tempo track from the full MIDI tempo map ===
  // SEQ tempo event i lasts step_time[i] ticks (it starts at the sum of the
  // previous step times).  Earlier versions kept only the first tempo, so
  // any song whose tempo changed (often a 120 BPM placeholder at tick 0)
  // played at the wrong speed.
  uint32_t first_musical_event_time = 0;
  for (int i = 0; i < event_count; i++) {
    if (events[i].status != 0x00) {
      first_musical_event_time = events[i].absolute_time;
      break;
    }
  }

  uint32_t total_song_time = 0;
  if (event_count > 0) {
    total_song_time = events[event_count - 1].absolute_time;
  }

  // Sort tempo points by time (stable insertion sort; file order breaks ties)
  // and keep only the last tempo at any given tick.
  for (int i = 1; i < tempo_count; i++) {
    TempoPoint tp = tempo_points[i];
    int j = i - 1;
    while (j >= 0 && tempo_points[j].time > tp.time) {
      tempo_points[j + 1] = tempo_points[j];
      j--;
    }
    tempo_points[j + 1] = tp;
  }
  int n_points = 0;
  for (int i = 0; i < tempo_count; i++) {
    if (n_points > 0 && tempo_points[n_points - 1].time == tempo_points[i].time)
      tempo_points[n_points - 1] = tempo_points[i];
    else
      tempo_points[n_points++] = tempo_points[i];
  }
  if (n_points == 0)
    printf("No tempo in MIDI file: using the MIDI default of 120 BPM.\n");

  // Segment boundaries: every tempo change inside the song, plus tick 0 and
  // the first musical event (the tempo loop offset points at that segment,
  // like the original two-event layout).
  int max_events = n_points + 3;
  SeqTempoEvent *tempo_events =
      (SeqTempoEvent *)malloc(sizeof(SeqTempoEvent) * (size_t)max_events);
  uint32_t *seg_start = (uint32_t *)malloc(sizeof(uint32_t) * (size_t)max_events);
  if (!tempo_events || !seg_start) {
    printf("Failed to allocate memory for tempo track.\n");
    return 1;
  }
  int seq_tempo_count = 0;
  int loop_index = 0;
  {
    uint32_t cur_mspb = 500000; // MIDI default tempo until the first Set Tempo
    int p = 0;
    // Tempo in effect at tick 0
    while (p < n_points && tempo_points[p].time == 0)
      cur_mspb = tempo_points[p++].mspb;
    seg_start[0] = 0;
    tempo_events[0].mspb = cur_mspb;
    seq_tempo_count = 1;

    bool split_done = (first_musical_event_time == 0);
    for (;;) {
      uint32_t next_change = (p < n_points) ? tempo_points[p].time : UINT32_MAX;
      if (!split_done && first_musical_event_time <= next_change) {
        // Boundary at the first musical event (same tempo continues)
        if (first_musical_event_time > seg_start[seq_tempo_count - 1]) {
          seg_start[seq_tempo_count] = first_musical_event_time;
          tempo_events[seq_tempo_count].mspb = cur_mspb;
          seq_tempo_count++;
        }
        loop_index = seq_tempo_count - 1;
        split_done = true;
        continue;
      }
      if (p >= n_points || next_change >= total_song_time)
        break;
      cur_mspb = tempo_points[p++].mspb;
      if (next_change == seg_start[seq_tempo_count - 1]) {
        tempo_events[seq_tempo_count - 1].mspb = cur_mspb;
      } else {
        seg_start[seq_tempo_count] = next_change;
        tempo_events[seq_tempo_count].mspb = cur_mspb;
        seq_tempo_count++;
      }
    }
    // Original layout: with music starting at tick 0, a zero-length lead-in
    // event precedes the main tempo event that the loop offset points to.
    if (first_musical_event_time == 0) {
      memmove(&tempo_events[1], &tempo_events[0], sizeof(SeqTempoEvent) * seq_tempo_count);
      memmove(&seg_start[1], &seg_start[0], sizeof(uint32_t) * seq_tempo_count);
      seq_tempo_count++;
      loop_index = 1;
    }
    for (int i = 0; i < seq_tempo_count; i++) {
      uint32_t end = (i + 1 < seq_tempo_count) ? seg_start[i + 1] : total_song_time;
      tempo_events[i].step_time = end > seg_start[i] ? end - seg_start[i] : 0;
    }
  }
  free(seg_start);
  free(tempo_points);
  if (seq_tempo_count > 4000) {
    printf("Error: %d tempo changes do not fit the SEQ header.\n", seq_tempo_count);
    return 1;
  }
  if (seq_tempo_count > 2)
    printf("Tempo map: %d tempo events.\n", seq_tempo_count);

  // === WRITE SEQ FILE ===
  FILE *seq_file = fopen(out_path, "wb");
  if (!seq_file) {
    perror("Error creating SEQ file");
    free(events);
    if (is_converted_fmt0)
      free(fmt0_data);
    free(input_data);
    return 1;
  }

  // --- Write Bank Header ---
  uint16_t num_songs = swap16(1);
  uint32_t song_ptr = swap32(6);
  fwrite(&num_songs, 2, 1, seq_file);
  fwrite(&song_ptr, 4, 1, seq_file);

  // --- Write SEQ Header ---
  SeqHeader seq_header = {0};
  seq_header.resolution = swap16((uint16_t)resolution);
  seq_header.num_tempo_events = swap16((uint16_t)seq_tempo_count);
  seq_header.data_offset = swap16((uint16_t)(8 + seq_tempo_count * 8));
  // Loop offset: the tempo event in effect when the music starts.
  seq_header.tempo_loop_offset = swap16((uint16_t)(8 + loop_index * 8));
  fwrite(&seq_header, sizeof(seq_header), 1, seq_file);

  // --- Write Tempo Track ---
  for (int i = 0; i < seq_tempo_count; i++) {
    uint32_t be_step = swap32(tempo_events[i].step_time);
    uint32_t be_mspb = swap32(tempo_events[i].mspb);
    fwrite(&be_step, 4, 1, seq_file);
    fwrite(&be_mspb, 4, 1, seq_file);
  }
  free(tempo_events);

  // --- Write Bank Select for all channels ---
  // The Saturn sound driver requires CC#32 (Bank Select LSB) to select
  // which tone bank to use.  Bank 1 is the user's tone data.
  // Without this, the driver defaults to bank 0 (driver internals).
  {
    for (int ch = 0; ch < 16; ch++) {
      uint8_t bank = g_gm_mode ? gm_initial_tone_bank(ch) : 1; // 1 = user tone data
      fputc(0xB0 | ch, seq_file); // CC status
      fputc(0x20, seq_file);      // CC#32 = Bank Select LSB
      fputc(bank, seq_file);      // Tone bank
      fputc(0x00, seq_file);      // Delta time = 0
    }
  }

  // --- With a voice map: initial program for every channel ---
  // A channel that never sends a program change plays GM program 0
  // (channel 10: standard drum kit), which in a merged TON is not voice 0.
  if (g_have_vmap) {
    for (int ch = 0; ch < 16; ch++) {
      fputc(0xC0 | ch, seq_file);
      fputc((uint8_t)map_program(ch, 0, 0), seq_file);
      fputc(0x00, seq_file); // Delta time = 0
    }
  }

  // --- Write Normal Track ---
  uint32_t last_event_time = 0;
  for (int i = 0; i < event_count; i++) {
    if (events[i].status == 0x00)
      continue; // Skip processed Note Off events

    uint32_t delta_time = events[i].absolute_time - last_event_time;
    last_event_time = events[i].absolute_time;

    write_large_delta_events(seq_file, &delta_time);

    uint8_t event_type = events[i].status & 0xF0;
    uint8_t channel = events[i].status & 0x0F;

    if (event_type == 0x90) { // Note On
      uint32_t gate_time = events[i].gate_time;
      write_extended_gate(seq_file, &gate_time);

      uint8_t ctl_byte = channel;
      if (delta_time >= 256) {
        ctl_byte |= 0x20;
        delta_time -= 256;
      }
      if (gate_time >= 256) {
        ctl_byte |= 0x40;
        gate_time -= 256;
      }

      fputc(ctl_byte, seq_file);
      fputc(events[i].data1, seq_file);
      fputc(events[i].data2, seq_file);
      fputc(gate_time, seq_file);
      fputc(delta_time, seq_file);

    } else { // Handle all other event types
      while (delta_time >= 256) {
        fputc(0x8C, seq_file);
        delta_time -= 256;
      }

      fputc(events[i].status, seq_file);

      if (event_type == 0xB0 || event_type == 0xA0) { // 2 data bytes
        fputc(events[i].data1, seq_file);
        fputc(events[i].data2, seq_file);
      } else if (event_type == 0xE0) {    // Pitch Bend
        fputc(events[i].data2, seq_file); // Use MSB (data2) as the value
      } else { // 1 data byte (Program Change, Channel Pressure)
        fputc(events[i].data1, seq_file);
      }
      fputc(delta_time, seq_file);
    }
  }
  fputc(0x83, seq_file); // End of track marker

  free(events);
  fclose(seq_file);

  if (is_converted_fmt0)
    free(fmt0_data);
  free(input_data);

  printf("Conversion complete.\n");
  return 0;
}
