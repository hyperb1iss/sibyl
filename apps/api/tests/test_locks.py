"""Tests for the distributed entity locking module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.locks import (
    LOCK_TTL_SECONDS,
    LOCK_WAIT_TIMEOUT,
    EntityLockManager,
    LockAcquisitionError,
    entity_lock,
    get_lock_manager,
    with_entity_lock,
)


class TestLockAcquisitionError:
    """Tests for LockAcquisitionError exception."""

    def test_error_message(self) -> None:
        """Error includes entity_id and reason."""
        error = LockAcquisitionError("entity_123", "org_456", "timeout")
        assert "entity_123" in str(error)
        assert "timeout" in str(error)

    def test_error_attributes(self) -> None:
        """Error stores entity_id, org_id, and reason."""
        error = LockAcquisitionError("entity_123", "org_456", "custom_reason")
        assert error.entity_id == "entity_123"
        assert error.org_id == "org_456"
        assert error.reason == "custom_reason"

    def test_default_reason(self) -> None:
        """Default reason is 'timeout'."""
        error = LockAcquisitionError("entity_123", "org_456")
        assert error.reason == "timeout"


class TestEntityLockManager:
    """Tests for EntityLockManager."""

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        """Create a mock Redis client."""
        redis = AsyncMock()
        redis.ping = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        redis.ttl = AsyncMock(return_value=30)
        redis.expire = AsyncMock(return_value=True)
        redis.eval = AsyncMock(return_value=1)
        redis.close = AsyncMock()
        return redis

    @pytest.fixture
    def manager(self) -> EntityLockManager:
        """Create a fresh lock manager."""
        return EntityLockManager()

    def test_lock_key_format(self, manager: EntityLockManager) -> None:
        """Lock key follows expected pattern."""
        key = manager._lock_key("org_123", "entity_456")
        assert key == "sibyl:lock:org_123:entity_456"

    def test_lock_value_format(self, manager: EntityLockManager) -> None:
        """Lock value includes instance ID and timestamp."""
        value = manager._lock_value()
        parts = value.split(":")
        assert len(parts) == 2
        # First part is instance ID (8 chars of UUID)
        assert len(parts[0]) == 8
        # Second part is timestamp (float as string)
        float(parts[1])  # Should not raise

    @pytest.mark.asyncio
    async def test_connect(self, manager: EntityLockManager, mock_redis: AsyncMock) -> None:
        """Manager connects to Redis."""
        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()
            mock_redis.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_idempotent(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Connect is idempotent - only connects once."""
        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()
            await manager.connect()  # Second call should be no-op
            mock_redis.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect(self, manager: EntityLockManager, mock_redis: AsyncMock) -> None:
        """Manager disconnects from Redis."""
        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()
            await manager.disconnect()
            mock_redis.close.assert_called_once()
            assert manager._redis is None

    @pytest.mark.asyncio
    async def test_acquire_success(self, manager: EntityLockManager, mock_redis: AsyncMock) -> None:
        """Acquire returns token on success."""
        mock_redis.set = AsyncMock(return_value=True)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            token = await manager.acquire("org_123", "entity_456")

        assert token is not None
        assert ":" in token  # Contains instance_id:timestamp
        mock_redis.set.assert_called_once()
        call_kwargs = mock_redis.set.call_args
        assert call_kwargs.kwargs["nx"] is True
        assert call_kwargs.kwargs["ex"] == LOCK_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_acquire_non_blocking_returns_none(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Non-blocking acquire returns None if lock held."""
        mock_redis.set = AsyncMock(return_value=False)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            token = await manager.acquire("org_123", "entity_456", blocking=False)

        assert token is None

    @pytest.mark.asyncio
    async def test_acquire_blocking_waits(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Blocking acquire waits and retries."""
        # First call fails, second succeeds
        mock_redis.set = AsyncMock(side_effect=[False, True])

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            token = await manager.acquire("org_123", "entity_456", wait_timeout=5.0)

        assert token is not None
        assert mock_redis.set.call_count == 2

    @pytest.mark.asyncio
    async def test_acquire_timeout_raises(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Blocking acquire raises LockAcquisitionError on timeout."""
        mock_redis.set = AsyncMock(return_value=False)

        with (
            patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis),
            pytest.raises(LockAcquisitionError) as exc_info,
        ):
            await manager.acquire("org_123", "entity_456", wait_timeout=0.1)

        assert exc_info.value.entity_id == "entity_456"
        assert exc_info.value.org_id == "org_123"
        assert exc_info.value.reason == "timeout"

    @pytest.mark.asyncio
    async def test_release_success(self, manager: EntityLockManager, mock_redis: AsyncMock) -> None:
        """Release returns True when lock is released."""
        mock_redis.eval = AsyncMock(return_value=1)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()
            released = await manager.release("org_123", "entity_456", "token_abc")

        assert released is True
        mock_redis.eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_not_owner(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Release returns False when not lock owner."""
        mock_redis.eval = AsyncMock(return_value=0)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()
            released = await manager.release("org_123", "entity_456", "wrong_token")

        assert released is False

    @pytest.mark.asyncio
    async def test_release_no_connection(self, manager: EntityLockManager) -> None:
        """Release returns False when not connected."""
        released = await manager.release("org_123", "entity_456", "token_abc")
        assert released is False

    @pytest.mark.asyncio
    async def test_extend_success(self, manager: EntityLockManager, mock_redis: AsyncMock) -> None:
        """Extend returns True when TTL is extended."""
        mock_redis.eval = AsyncMock(return_value=1)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()
            extended = await manager.extend("org_123", "entity_456", "token_abc")

        assert extended is True

    @pytest.mark.asyncio
    async def test_extend_not_owner(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Extend returns False when not lock owner."""
        mock_redis.eval = AsyncMock(return_value=0)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()
            extended = await manager.extend("org_123", "entity_456", "wrong_token")

        assert extended is False

    @pytest.mark.asyncio
    async def test_lock_context_manager(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Lock context manager acquires and releases lock."""
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.eval = AsyncMock(return_value=1)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            async with manager.lock("org_123", "entity_456") as token:
                assert token is not None

        # Verify release was called
        mock_redis.eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_lock_context_manager_releases_on_exception(
        self, manager: EntityLockManager, mock_redis: AsyncMock
    ) -> None:
        """Lock is released even if exception occurs inside context."""
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.eval = AsyncMock(return_value=1)

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            try:
                async with manager.lock("org_123", "entity_456") as token:
                    assert token is not None
                    raise ValueError("Test error")
            except ValueError:
                pass  # Expected

        # Verify release was called
        mock_redis.eval.assert_called_once()


class TestEntityLockConvenience:
    """Tests for convenience functions."""

    @pytest.fixture
    def mock_manager(self) -> MagicMock:
        """Create mock lock manager."""
        manager = MagicMock()
        manager.lock = MagicMock()
        return manager

    @pytest.mark.asyncio
    async def test_entity_lock_uses_global_manager(self) -> None:
        """entity_lock uses the global lock manager."""
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value="token_123")
        mock_lock.__aexit__ = AsyncMock(return_value=None)

        mock_manager = MagicMock()
        mock_manager.lock = MagicMock(return_value=mock_lock)

        with patch("sibyl.locks.get_lock_manager", return_value=mock_manager):
            async with entity_lock("org_123", "entity_456") as token:
                assert token == "token_123"

        mock_manager.lock.assert_called_once_with("org_123", "entity_456", LOCK_WAIT_TIMEOUT, True)

    @pytest.mark.asyncio
    async def test_entity_lock_passes_parameters(self) -> None:
        """entity_lock passes wait_timeout and blocking parameters."""
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value="token_123")
        mock_lock.__aexit__ = AsyncMock(return_value=None)

        mock_manager = MagicMock()
        mock_manager.lock = MagicMock(return_value=mock_lock)

        with patch("sibyl.locks.get_lock_manager", return_value=mock_manager):
            async with entity_lock("org_123", "entity_456", wait_timeout=10.0, blocking=False):
                pass

        mock_manager.lock.assert_called_once_with("org_123", "entity_456", 10.0, False)


class TestWithEntityLockDecorator:
    """Tests for with_entity_lock decorator."""

    @pytest.mark.asyncio
    async def test_decorator_acquires_lock(self) -> None:
        """Decorator acquires lock before function execution."""
        lock_acquired = False

        mock_lock = AsyncMock()

        async def mock_aenter(*args: object) -> str:
            nonlocal lock_acquired
            lock_acquired = True
            return "token_123"

        mock_lock.__aenter__ = mock_aenter
        mock_lock.__aexit__ = AsyncMock(return_value=None)

        mock_manager = MagicMock()
        mock_manager.lock = MagicMock(return_value=mock_lock)

        with patch("sibyl.locks.get_lock_manager", return_value=mock_manager):

            @with_entity_lock()
            async def my_function(org_id: str, entity_id: str) -> str:
                assert lock_acquired
                return "result"

            result = await my_function(org_id="org_123", entity_id="entity_456")
            assert result == "result"

    @pytest.mark.asyncio
    async def test_decorator_custom_arg_names(self) -> None:
        """Decorator supports custom argument names."""
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value="token_123")
        mock_lock.__aexit__ = AsyncMock(return_value=None)

        mock_manager = MagicMock()
        mock_manager.lock = MagicMock(return_value=mock_lock)

        with patch("sibyl.locks.get_lock_manager", return_value=mock_manager):

            @with_entity_lock(org_id_arg="organization", entity_id_arg="task_id")
            async def update_task(organization: str, task_id: str) -> str:
                return "updated"

            result = await update_task(organization="org_123", task_id="task_456")
            assert result == "updated"

    @pytest.mark.asyncio
    async def test_decorator_missing_args_raises(self) -> None:
        """Decorator raises ValueError if required args missing."""

        @with_entity_lock()
        async def my_function(org_id: str) -> str:
            return "result"

        with pytest.raises(ValueError) as exc_info:
            await my_function(org_id="org_123")

        assert "entity_id" in str(exc_info.value)


class TestConcurrentLocking:
    """Tests for concurrent lock behavior."""

    @pytest.mark.asyncio
    async def test_concurrent_acquire_serializes(self) -> None:
        """Concurrent acquires are serialized - only one succeeds immediately."""
        acquire_order: list[int] = []
        release_order: list[int] = []

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()
        mock_redis.close = AsyncMock()

        # Simulate lock contention: first call succeeds, rest fail until release
        lock_held = False

        async def mock_set(*args: object, **kwargs: object) -> bool:
            nonlocal lock_held
            if not lock_held:
                lock_held = True
                return True
            return False

        async def mock_eval(*args: object, **kwargs: object) -> int:
            nonlocal lock_held
            lock_held = False
            return 1

        mock_redis.set = mock_set
        mock_redis.eval = mock_eval

        manager = EntityLockManager()

        with patch("sibyl.coordination._redis.locks.Redis", return_value=mock_redis):
            await manager.connect()

            async def worker(worker_id: int) -> None:
                async with manager.lock("org", "entity", wait_timeout=5.0) as token:
                    if token:
                        acquire_order.append(worker_id)
                        await asyncio.sleep(0.05)  # Simulate work
                        release_order.append(worker_id)

            # Start multiple workers concurrently
            await asyncio.gather(worker(1), worker(2), worker(3))

        # All workers should complete
        assert len(acquire_order) == 3
        assert len(release_order) == 3
        # Order should be consistent (FIFO-ish with some jitter)
        assert set(acquire_order) == {1, 2, 3}


class TestGlobalLockManager:
    """Tests for global lock manager singleton."""

    def test_get_lock_manager_returns_same_instance(self) -> None:
        """get_lock_manager returns the same instance."""
        # Reset global state
        import sibyl.locks

        sibyl.locks._lock_manager = None

        manager1 = get_lock_manager()
        manager2 = get_lock_manager()
        assert manager1 is manager2

        # Cleanup
        sibyl.locks._lock_manager = None

    def test_get_lock_manager_creates_if_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_lock_manager creates the Redis-backed EntityLockManager in legacy mode."""
        import sibyl.coordination.locks as coord_locks
        import sibyl.locks

        monkeypatch.setattr(coord_locks, "get_coordination_backend", lambda: "redis")
        sibyl.locks._lock_manager = None
        coord_locks._locks = None
        coord_locks._locks_backend = None

        manager = get_lock_manager()
        assert manager is not None
        assert isinstance(manager, EntityLockManager)

        # Cleanup
        sibyl.locks._lock_manager = None
        coord_locks._locks = None
        coord_locks._locks_backend = None
