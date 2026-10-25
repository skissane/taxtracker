"""Shared behaviour for self-referential tree models (e.g. Item, Task).

`TreeNodeMixin` expects the model to have a nullable self-referential `parent`
ForeignKey, a `title` and a `status` using `TreeNodeMixin.STATUS_CHOICES`.
"""

from django.core.exceptions import ValidationError

CYCLE_MARKER = "…"


class TreeNodeMixin:
    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_DONE, "Done"),
    ]

    @property
    def is_done(self):
        return self.status == self.STATUS_DONE

    def ancestor_titles(self, cycle_marker=None):
        """Return the titles from the root down to this node.

        Stops at a parent cycle (which `check_parent_cycle` prevents, but may
        already exist in the DB), starting the list with `cycle_marker` if given.
        """
        titles = [self.title]
        visited = {self.pk}
        current = self
        while current.parent_id is not None:
            if current.parent_id in visited:
                if cycle_marker is not None:
                    titles.insert(0, cycle_marker)
                break
            visited.add(current.parent_id)
            current = current.parent
            titles.insert(0, current.title)
        return titles

    def tree_path_str(self):
        """Return the path as "A > B > C", with "…" at the start on a cycle."""
        return " > ".join(self.ancestor_titles(cycle_marker=CYCLE_MARKER))

    def get_folder_path(self):
        """Return a list of titles representing the path from root to this node."""
        return self.ancestor_titles()

    def check_parent_cycle(self):
        """Raise ValidationError (on `parent`) if `parent` would create a cycle."""
        if self.parent_id is None or self.pk is None:
            return
        manager = type(self)._default_manager
        # If the parent_id is unchanged from the persisted value, a cycle
        # already exists in the DB and we are not making it worse — skip
        # detection so that existing cycles don't block every subsequent save
        # (e.g. inline formsets re-validate unchanged rows, which would
        # otherwise surface an invisible "Please correct the error below"
        # message when the parent field is hidden).
        original_parent_id = manager.filter(pk=self.pk).values_list("parent_id", flat=True).first()
        if original_parent_id == self.parent_id:
            return
        # Walk the ancestor chain; if we encounter self, there is a cycle.
        visited = {self.pk}
        current_id = self.parent_id
        while current_id is not None:
            if current_id in visited:
                raise ValidationError({"parent": "Setting this parent would create a circular reference."})
            visited.add(current_id)
            current_id = manager.filter(pk=current_id).values_list("parent_id", flat=True).first()
