-- KEYS[1]=cdp:rl:{tokenId}  KEYS[2]=cdp:quota:d:{tokenId}:{yyyyMMdd}  KEYS[3]=cdp:quota:m:{tokenId}:{yyyyMM}
-- ARGV[1]=tps ARGV[2]=burst ARGV[3]=now_ms ARGV[4]=dailyQuota ARGV[5]=monthlyQuota
-- ARGV[6]=dailyTtl ARGV[7]=monthlyTtl
-- Returns: {allowed:int, reason:string, remaining_daily:int}
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local ts     = tonumber(redis.call('HGET', KEYS[1], 'ts'))
local tps    = tonumber(ARGV[1])
local burst  = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])

if tokens == nil then tokens = burst; ts = now end
local refill = (now - ts) / 1000 * tps
tokens = math.min(burst, tokens + refill)

if tokens < 1 then
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', KEYS[1], 60)
  return {0, 'RATE_LIMIT', 0}
end

local d = tonumber(redis.call('GET', KEYS[2]) or '0')
if d >= tonumber(ARGV[4]) then return {0, 'DAILY_QUOTA', 0} end
local m = tonumber(redis.call('GET', KEYS[3]) or '0')
if m >= tonumber(ARGV[5]) then return {0, 'MONTHLY_QUOTA', 0} end

tokens = tokens - 1
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 60)
d = redis.call('INCR', KEYS[2]); redis.call('EXPIRE', KEYS[2], tonumber(ARGV[6]))
m = redis.call('INCR', KEYS[3]); redis.call('EXPIRE', KEYS[3], tonumber(ARGV[7]))

return {1, 'OK', tonumber(ARGV[4]) - d}
