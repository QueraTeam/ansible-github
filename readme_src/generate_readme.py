import os
import pathlib
import subprocess
import json
from jinja2 import Template
from pytablewriter import MarkdownTableWriter


def main():
    collection_path = pathlib.Path(__file__).parent.parent.resolve()
    os.environ["ANSIBLE_LIBRARY"] = str(collection_path / "plugins/modules")
    module_meta = json.loads(
        subprocess.check_output(
            ["ansible-doc", "-t", "module", "--json", "install_from_github"], text=True
        )
    ).get("install_from_github")

    writer = MarkdownTableWriter(
        # table_name="module options",
        headers=["Parameter", "Type", "Description"],
        value_matrix=[
            [
                k,
                Template(
                    """
Type: `{{ type }}`
{% if required %}<br/>**Required**{% endif %}
{% if default %}<br/>Default: `{{ default }}`{% endif %}"""
                ).render(key=k, **v),
                "<br/>".join(v["description"]),
            ]
            for k, v in module_meta["doc"]["options"].items()
        ],
    )
    options_md_table = writer.dumps()

    with open(collection_path / "readme_src/README.md.jinja2", "r") as fp:
        readme_template = fp.read()
    template = Template(readme_template)
    readme_rendered = template.render(
        doc=module_meta["doc"],
        options_md_table=options_md_table,
        examples=module_meta["examples"],
    )

    with open(collection_path / "README.md", "w") as fp:
        fp.write(readme_rendered)


if __name__ == "__main__":
    main()
